"""Chunked uploads from the console into a running agent's workspace.

A file the operator uploads arrives as `upload_begin` {agent_id, path, size},
then `upload_chunk` {upload_id, seq, data} in order (each at most
`CHUNK_MAX_BYTES`, base64), then `upload_end` {upload_id, sha256}; or
`upload_abort` {upload_id}. Each chunk is appended to `<path>.partial-<id>` in
the bottle through `Bottle.exec` (never a host path). `upload_end` checks the
size and SHA-256 (here, over what was written, and in the guest when it has
`sha256sum`), then moves the partial into place without overwriting: a name
that's taken becomes `name-1.ext`, `name-2.ext`, … and the reply says which.

An upload is bound to the agent that began it, at most `HOST_UPLOAD_MAX_BYTES`
(the console asks for less), and dropped (its partial removed) after
`IDLE_SECONDS` without a chunk. Paths are checked as for `put_workspace_file`
(`workspace_files._safe_upload_path`): only under `/home/node/uploads`, and the
guest checks the prefix again. The one-message `put_workspace_file` stays for
older consoles."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import secrets
import shlex
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .workspace_files import UPLOAD_ROOT, WorkspaceFileError, _safe_upload_path

if TYPE_CHECKING:
    from ...backend.base import Bottle

# The largest file this host takes, whatever the console's limit.
HOST_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
# The largest decoded chunk (its base64 is about 5.3 MB on the link).
CHUNK_MAX_BYTES = 4 * 1024 * 1024
# An upload with no chunk for this long is dropped.
IDLE_SECONDS = 120.0
# Uploads in flight at once on this host.
MAX_ACTIVE = 8
EXEC_TIMEOUT_SECONDS = 60.0

_ERRORS = {"outside": "that path is outside the upload directory",
           "mkdir": "couldn't create the upload directory in the bottle",
           "write": "couldn't write the file in the bottle",
           "size": "the file in the bottle isn't the size that was sent",
           "hash": "the file in the bottle doesn't match what was sent",
           "names": "too many files with that name in the upload directory",
           "move": "couldn't move the file into place in the bottle"}


class UploadError(Exception):
    """Why an upload step was refused; the message goes to the console."""


@dataclass
class _Upload:
    upload_id: str
    agent_id: str
    bottle: Any
    path: str
    partial: str
    size: int
    received: int = 0
    next_seq: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    last: float = 0.0


def _guard(var: str, root: str) -> list[str]:
    """Script lines refusing `$var` outside the upload root (the guest's check)."""
    return [f'case "${var}" in', f"  {shlex.quote(root.rstrip('/'))}/*) ;;",
            '  *) echo "E outside"; exit 0;;', "esac"]


def begin_script(partial: str, root: str = UPLOAD_ROOT) -> str:
    return "\n".join([f"t={shlex.quote(partial)}", *_guard("t", root),
                      'mkdir -p "$(dirname "$t")" 2>/dev/null || { echo "E mkdir"; exit 0; }',
                      ': > "$t" 2>/dev/null && echo OK || echo "E write"'])


def chunk_script(partial: str, data_b64: str, root: str = UPLOAD_ROOT) -> str:
    # data_b64 is only A-Za-z0-9+/=, safe in single quotes.
    return "\n".join([f"t={shlex.quote(partial)}", *_guard("t", root),
                      f"printf '%s' {shlex.quote(data_b64)} | base64 -d >> \"$t\" "
                      "&& echo OK || echo 'E write'"])


def end_script(partial: str, path: str, size: int, sha256: str,
               root: str = UPLOAD_ROOT) -> str:
    """Check the partial's size (and hash, with `sha256sum`), then move it to
    `path`, or the first of `stem-1.ext`, `stem-2.ext`… that's free; prints
    `OK <final path>`."""
    return "\n".join([
        f"t={shlex.quote(partial)}", f"p={shlex.quote(path)}", *_guard("t", root),
        *_guard("p", root),
        f'[ "$(wc -c < "$t" | tr -d " ")" = {size} ] || {{ echo "E size"; exit 0; }}',
        "if command -v sha256sum >/dev/null 2>&1; then",
        f'  [ "$(sha256sum "$t" | cut -d" " -f1)" = {shlex.quote(sha256)} ] '
        '|| { echo "E hash"; exit 0; }',
        "fi",
        'd=$(dirname "$p"); b=$(basename "$p")',
        'case "$b" in ?*.*) stem="${b%.*}"; ext=".${b##*.}";; *) stem="$b"; ext="";; esac',
        'f="$p"; n=1',
        'while [ -e "$f" ]; do',
        '  f="$d/$stem-$n$ext"; n=$((n + 1))',
        '  [ "$n" -gt 1000 ] && { echo "E names"; exit 0; }',
        "done",
        'mv -n "$t" "$f" 2>/dev/null && [ ! -e "$t" ] && echo "OK $f" || echo "E move"',
    ])


def abort_script(partial: str, root: str = UPLOAD_ROOT) -> str:
    return "\n".join([f"t={shlex.quote(partial)}", *_guard("t", root), 'rm -f "$t"',
                      "echo OK"])


def _run(bottle: "Bottle", script: str) -> str:
    """The script's `OK …` line (after `OK `), or UploadError for its `E …`."""
    r = bottle.exec(script, timeout=EXEC_TIMEOUT_SECONDS)
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        if line == "OK" or line.startswith("OK "):
            return line[3:]
        if line.startswith("E "):
            raise UploadError(_ERRORS.get(line[2:], "couldn't write the file in the bottle"))
    raise UploadError("couldn't write the file in the bottle")


class Uploads:
    """The uploads in flight on this host, keyed by an unguessable id."""

    def __init__(self, attach: Callable[[str], Any], *, root: str = UPLOAD_ROOT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._attach, self._clock, self._root = attach, clock, root
        self._lock = threading.Lock()
        self._uploads: dict[str, _Upload] = {}

    def _sweep(self) -> None:
        now = self._clock()
        with self._lock:
            stale = [u for u in self._uploads.values() if now - u.last > IDLE_SECONDS]
            for u in stale:
                del self._uploads[u.upload_id]
        for u in stale:
            try:
                _run(u.bottle, abort_script(u.partial, self._root))
            except UploadError:
                pass  # the bottle may be gone; nothing else to clean

    def _get(self, upload_id: object, agent_id: str) -> _Upload:
        with self._lock:
            u = self._uploads.get(str(upload_id))
        if u is None or u.agent_id != agent_id:
            raise UploadError("no such upload (it may have timed out); start it again")
        return u

    def begin(self, agent_id: str, identity: str, path: object, size: object) -> dict[str, object]:
        self._sweep()
        if not isinstance(path, str):
            raise UploadError("that isn't a file path")
        try:
            _safe_upload_path(path, self._root)
        except WorkspaceFileError as e:
            raise UploadError(str(e)) from e
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise UploadError("the upload needs its size")
        if size > HOST_UPLOAD_MAX_BYTES:
            raise UploadError(f"this host takes uploads up to "
                              f"{HOST_UPLOAD_MAX_BYTES // (1024 * 1024)} MiB")
        with self._lock:
            if len(self._uploads) >= MAX_ACTIVE:
                raise UploadError("too many uploads in progress on this host; try again shortly")
        attached = self._attach(identity)
        if isinstance(attached, str):
            raise UploadError(attached)
        upload_id = secrets.token_hex(8)
        u = _Upload(upload_id, agent_id, attached[0], path, f"{path}.partial-{upload_id}",
                    size, last=self._clock())
        _run(u.bottle, begin_script(u.partial, self._root))
        with self._lock:
            self._uploads[upload_id] = u
        return {"upload_id": upload_id, "max_bytes": HOST_UPLOAD_MAX_BYTES,
                "chunk_bytes": CHUNK_MAX_BYTES}

    def chunk(self, agent_id: str, upload_id: object, seq: object,
              data_b64: object) -> dict[str, object]:
        u = self._get(upload_id, agent_id)
        if seq != u.next_seq:
            raise UploadError(f"expected chunk {u.next_seq}, got {seq!r}")
        if not isinstance(data_b64, str):
            raise UploadError("the chunk has no data")
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise UploadError("the chunk is not valid base64") from e
        if len(raw) > CHUNK_MAX_BYTES:
            raise UploadError(f"a chunk is at most {CHUNK_MAX_BYTES // (1024 * 1024)} MiB")
        if u.received + len(raw) > u.size:
            raise UploadError("more data than the upload's size")
        _run(u.bottle, chunk_script(u.partial, data_b64, self._root))
        u.digest.update(raw)
        u.received += len(raw)
        u.next_seq += 1
        u.last = self._clock()
        return {"received": u.received}

    def end(self, agent_id: str, upload_id: object, sha256: object) -> dict[str, object]:
        u = self._get(upload_id, agent_id)
        with self._lock:
            self._uploads.pop(u.upload_id, None)
        try:
            if u.received != u.size:
                raise UploadError(f"got {u.received} of {u.size} bytes")
            if not isinstance(sha256, str) or sha256.lower() != u.digest.hexdigest():
                raise UploadError("the file's SHA-256 doesn't match what arrived")
            final = _run(u.bottle, end_script(u.partial, u.path, u.size,
                                              u.digest.hexdigest(), self._root))
        except UploadError:
            self._drop(u)
            raise
        return {"path": final, "size": u.size}

    def abort(self, agent_id: str, upload_id: object) -> dict[str, object]:
        u = self._get(upload_id, agent_id)
        with self._lock:
            self._uploads.pop(u.upload_id, None)
        self._drop(u)
        return {"aborted": True}

    def _drop(self, u: _Upload) -> None:
        try:
            _run(u.bottle, abort_script(u.partial, self._root))
        except UploadError:
            pass  # best effort: the partial goes with the bottle anyway


_REGISTRY: dict[int, Uploads] = {}


def _uploads_for(bottles: Any) -> Uploads | None:
    """The `Uploads` for this host's `BottleService` (one per service)."""
    attach = getattr(bottles, "attached", None)
    if attach is None:
        return None
    return _REGISTRY.setdefault(id(bottles), Uploads(attach))


_REPLY = {"upload_begin": "upload_begun", "upload_chunk": "upload_chunk_ack",
          "upload_end": "upload_done", "upload_abort": "upload_aborted"}


async def handle(msg: Mapping[str, Any], running: Mapping[str, str],
                 bottles: Any) -> dict[str, Any]:
    """One upload request from the console (`remote_actions.QUERIES`)."""
    kind = str(msg.get("type", ""))
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": _REPLY.get(kind, "upload_error"),
                             "request_id": msg.get("request_id"), "agent_id": agent_id}
    uploads = _uploads_for(bottles)
    identity = running.get(agent_id)
    try:
        if uploads is None:
            raise UploadError("this host controller can't take chunked uploads")
        if not identity:
            raise UploadError("that agent isn't running on this host")
        if kind == "upload_begin":
            result = await asyncio.to_thread(uploads.begin, agent_id, identity,
                                             msg.get("path"), msg.get("size"))
        elif kind == "upload_chunk":
            result = await asyncio.to_thread(uploads.chunk, agent_id, msg.get("upload_id"),
                                             msg.get("seq"), msg.get("data"))
        elif kind == "upload_end":
            result = await asyncio.to_thread(uploads.end, agent_id, msg.get("upload_id"),
                                             msg.get("sha256"))
        else:
            result = await asyncio.to_thread(uploads.abort, agent_id, msg.get("upload_id"))
    except UploadError as e:
        result = {"error": str(e)}
    return {**reply, "upload_id": msg.get("upload_id"), **result}


QUERIES = {kind: handle for kind in _REPLY}

__all__ = ["CHUNK_MAX_BYTES", "HOST_UPLOAD_MAX_BYTES", "IDLE_SECONDS", "QUERIES", "UploadError",
           "Uploads", "abort_script", "begin_script", "chunk_script", "end_script", "handle"]
