"""Copy each session's harness transcript to the host as it's written (PRD 0095).

A follower per session polls the bottle once a second through `exec` (and
once more after the agent exits), reads new bytes by offset from each
transcript file that appeared since the session started, and appends them
exactly as read to

  ~/.bot-bottle/transcripts/<session>/<activation>/<file>.jsonl   (0600)

with `<file>.meta.json` beside it (the guest path, the offset read so far and
the console agent id). `follow.json` in the same directory holds what a
restarted host controller needs to resume: the guest glob, the files that
were there before the session (never copied) and the agent id.

The copy is the agent's account, not evidence: `transcript.captured` records
its running SHA-256 (agent-claimed) every five minutes while it grows and when
it closes. A guest file that shrinks or changes before the offset already
read gets a fresh copy, read from its start, and a `transcript.rewritten`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...paths import bot_bottle_root

# An exec reading the guest's transcripts gives up after this long.
POLL_TIMEOUT_SECONDS = 60.0

if TYPE_CHECKING:
    from ...backend import Bottle
    from .audit_events import HostAudit

POLL_SECONDS = 1.0
CHECKPOINT_SECONDS = 300.0
# Bytes read per file per poll; a longer backlog takes a few polls.
READ_LIMIT = 4 * 1024 * 1024
# Bytes before the offset compared each poll, to notice an edit.
CHECK_BYTES = 64
_FOLLOW = "follow.json"


def transcripts_root() -> Path:
    return bot_bottle_root() / "transcripts"


def copy_dir(session: str, activation: str) -> Path:
    return transcripts_root() / session / activation


def _make_dir(path: Path) -> None:
    """`path` and the transcripts dirs above it, 0700."""
    root = transcripts_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in reversed([path, *path.parents]):
        if p.is_relative_to(root) and p != root:
            p.mkdir(exist_ok=True, mode=0o700)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically, 0600."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def shell_glob(pattern: str) -> str:
    """`pattern` for the shell: `*` and `?` stay wild, the rest is quoted."""
    return "".join(p if p in ("*", "?") else shlex.quote(p)
                   for p in re.split(r"([*?])", pattern) if p)


@dataclass
class _Copy:
    """One host copy of (part of) one guest file."""

    path: Path
    guest_path: str
    offset: int = 0
    tail: bytes = b""  # the last bytes read, to notice an edit
    sha: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    checkpointed_size: int = -1
    checkpointed_at: float = 0.0
    opened: float = 0.0  # wall clock; the run's first file is the one shown

    @property
    def meta_path(self) -> Path:
        return self.path.with_name(self.path.name.removesuffix(".jsonl") + ".meta.json")


@dataclass(frozen=True)
class Listing:
    """What one poll found of one guest file."""

    guest_path: str
    size: int
    check: bytes | None
    data: bytes


def poll_script(glob: str, skip: list[str], known: dict[str, int]) -> str:
    """The `exec` script for one poll: for each matching file not in `skip`,
    `F <size> <path>`, then (for a file read before) `C <base64>` of the
    CHECK_BYTES before its offset, then `D <base64>` of up to READ_LIMIT new
    bytes."""
    cases = [f"    {shlex.quote(p)}) continue;;" for p in skip]
    for path, offset in known.items():
        cases.append(f"    {shlex.quote(path)}) off={offset};;")
    return "\n".join([
        f"for f in {shell_glob(glob)}; do",
        '  [ -f "$f" ] || continue',
        '  case "$f" in',
        *cases,
        "    *) off=0;;",
        "  esac",
        '  size=$(wc -c < "$f" | tr -d " ")',
        '  printf "F %s %s\\n" "$size" "$f"',
        f'  chk=$(( off < {CHECK_BYTES} ? off : {CHECK_BYTES} ))',
        '  if [ "$chk" -gt 0 ] && [ "$size" -ge "$off" ]; then',
        '    printf "C "',
        '    tail -c +$((off - chk + 1)) "$f" | head -c "$chk" | base64 | tr -d "\\n"',
        '    printf "\\n"',
        "  fi",
        '  if [ "$size" -gt "$off" ]; then',
        '    printf "D "',
        f'    tail -c +$((off + 1)) "$f" | head -c {READ_LIMIT} | base64 | tr -d "\\n"',
        '    printf "\\n"',
        "  fi",
        "done",
    ])


def parse_listing(stdout: str) -> list[Listing]:
    found: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "F":
            size, _, path = rest.partition(" ")
            found.append({"guest_path": path, "size": int(size or 0), "check": None,
                          "data": b""})
        elif found and tag == "C":
            found[-1]["check"] = base64.b64decode(rest)
        elif found and tag == "D":
            found[-1]["data"] = base64.b64decode(rest)
    return [Listing(**f) for f in found]


class TranscriptFollower:
    """Follows one session's transcript files into host copies."""

    def __init__(self, *, session: str, activation: str, glob: str, agent_id: str,
                 skip: list[str], audit: "HostAudit", harness: str = "",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session, self.activation = session, activation
        self.glob, self.agent_id, self.skip = glob, agent_id, skip
        self.harness = harness
        self.dir = copy_dir(session, activation)
        self._audit = audit
        self._clock = clock
        self._copies: dict[str, _Copy] = {}  # guest path -> its current copy
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._bottle: "Bottle | None" = None

    # --- creating and resuming ---

    @classmethod
    def begin(cls, bottle: "Bottle", *, session: str, activation: str, glob: str,
              agent_id: str, audit: "HostAudit", harness: str = "") -> "TranscriptFollower":
        """A follower for a session about to start: files already there are
        another session's and never copied."""
        r = bottle.exec(f'for f in {shell_glob(glob)}; do '
                        '[ -f "$f" ] && printf "%s\\n" "$f"; done; :',
                        timeout=POLL_TIMEOUT_SECONDS)
        skip = r.stdout.splitlines() if r.returncode == 0 else []
        follower = cls(session=session, activation=activation, glob=glob, agent_id=agent_id,
                       skip=skip, audit=audit, harness=harness)
        _make_dir(follower.dir)
        follower._save_follow()
        return follower

    @classmethod
    def resume(cls, *, session: str, activation: str,
               audit: "HostAudit") -> "TranscriptFollower | None":
        """The follower a previous host controller left, from its saved
        offsets; None when it followed nothing."""
        follow = _read_json(copy_dir(session, activation) / _FOLLOW)
        if follow is None or follow.get("closed"):
            return None
        follower = cls(session=session, activation=activation, glob=str(follow.get("glob", "")),
                       agent_id=str(follow.get("agent_id", "")),
                       skip=[str(p) for p in follow.get("skip", [])], audit=audit,
                       harness=str(follow.get("harness", "")))
        for meta_path in sorted(follower.dir.glob("*.meta.json")):
            meta = _read_json(meta_path)
            if meta is None or meta.get("closed"):
                continue
            copy = follower._load(meta_path.with_name(
                meta_path.name.removesuffix(".meta.json") + ".jsonl"), meta)
            follower._copies[copy.guest_path] = copy
        return follower

    def _load(self, path: Path, meta: dict[str, Any]) -> _Copy:
        copy = _Copy(path, str(meta.get("guest_path", "")), offset=int(meta.get("offset", 0)),
                     opened=float(meta.get("opened", 0.0)))
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        copy.sha.update(data)
        copy.size = len(data)
        copy.tail = data[-CHECK_BYTES:]
        return copy

    # --- running ---

    def start(self, bottle: "Bottle") -> None:
        self._bottle = bottle
        self._thread = threading.Thread(target=self._run, name=f"transcript-{self.session}",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            self.poll()

    def detach(self) -> None:
        """Stop polling and leave the copies open, as a host controller that
        exits does; the next one resumes them."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def close(self, bottle: "Bottle | None" = None) -> None:
        """Stop polling, read once more (when there's a bottle to read from),
        and close every copy with a final checkpoint."""
        self.detach()
        bottle = bottle or self._bottle
        if bottle is not None:
            self.poll(bottle)
        with self._lock:
            for copy in self._copies.values():
                self._close_copy(copy)
            self._copies.clear()
        if self.dir.is_dir():
            self._save_follow(closed=True)

    def poll(self, bottle: "Bottle | None" = None) -> None:
        """Read what's new. Failures are logged and retried next poll."""
        bottle = bottle or self._bottle
        if bottle is None:
            return
        with self._lock:
            known = {p: c.offset for p, c in self._copies.items()}
            try:
                # Bounded: a poll that never returned held the lock, and
                # `close` (so the bottle's teardown) waited on it for good.
                r = bottle.exec(poll_script(self.glob, self.skip, known),
                                timeout=POLL_TIMEOUT_SECONDS)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.debug(f"transcript {self.session}: poll failed: {e}")
                return
            if r.returncode != 0:
                log.debug(f"transcript {self.session}: poll failed: {r.stderr.strip()[:200]}")
                return
            for listing in parse_listing(r.stdout):
                self._take(listing)
            now = self._clock()
            for copy in self._copies.values():
                if copy.size != copy.checkpointed_size and \
                        now - copy.checkpointed_at >= CHECKPOINT_SECONDS:
                    self._checkpoint(copy, final=False)

    def _take(self, listing: Listing) -> None:
        copy = self._copies.get(listing.guest_path)
        if copy is None:
            copy = self._new_copy(listing.guest_path)
        elif listing.size < copy.offset or (
                listing.check is not None and copy.tail
                and listing.check != copy.tail[-len(listing.check):]):
            # Edited after we read it: keep what we have, copy it afresh. The
            # new bytes are from the old offset, so read again from the start.
            copy = self._rewritten(copy, listing)
            return
        if listing.data:
            self._append(copy, listing.data)

    def _copy_path(self, guest_path: str, suffix: str = "") -> Path:
        stem = Path(guest_path).name.removesuffix(".jsonl") or "transcript"
        return self.dir / f"{stem}{suffix}.jsonl"

    def _new_copy(self, guest_path: str, suffix: str = "") -> _Copy:
        _make_dir(self.dir)
        copy = _Copy(self._copy_path(guest_path, suffix), guest_path, opened=time.time())
        os.close(os.open(copy.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        copy.checkpointed_size, copy.checkpointed_at = 0, self._clock()
        self._copies[guest_path] = copy
        self._save_meta(copy)
        return copy

    def _append(self, copy: _Copy, data: bytes) -> None:
        fd = os.open(copy.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        copy.sha.update(data)
        copy.size += len(data)
        copy.offset += len(data)
        copy.tail = (copy.tail + data)[-CHECK_BYTES:]
        self._save_meta(copy)

    def _rewritten(self, old: _Copy, listing: Listing) -> _Copy:
        self._close_copy(old)
        n = 1
        while self._copy_path(listing.guest_path, f".rewrite-{n}").exists():
            n += 1
        new = self._new_copy(listing.guest_path, suffix=f".rewrite-{n}")
        self._audit.transcript_rewritten(
            bottle=self.session, activation=self.activation, copy=self._rel(old.path),
            new_copy=self._rel(new.path), guest_path=listing.guest_path,
            offset=old.offset, size=listing.size)
        return new

    def _close_copy(self, copy: _Copy) -> None:
        self._checkpoint(copy, final=True)
        self._save_meta(copy, closed=True)

    def _checkpoint(self, copy: _Copy, *, final: bool) -> None:
        copy.checkpointed_size, copy.checkpointed_at = copy.size, self._clock()
        self._audit.transcript_captured(
            bottle=self.session, activation=self.activation, copy=self._rel(copy.path),
            guest_path=copy.guest_path, size=copy.size,
            sha256=copy.sha.hexdigest(), final=final)

    def _save_meta(self, copy: _Copy, *, closed: bool = False) -> None:
        meta: dict[str, Any] = {"guest_path": copy.guest_path, "offset": copy.offset,
                                "agent_id": self.agent_id, "opened": copy.opened}
        if closed:
            meta["closed"] = True
        _write_json(copy.meta_path, meta)

    def _save_follow(self, *, closed: bool = False) -> None:
        follow: dict[str, Any] = {"glob": self.glob, "skip": self.skip,
                                  "agent_id": self.agent_id, "harness": self.harness}
        if closed:
            follow["closed"] = True
        _write_json(self.dir / _FOLLOW, follow)

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(transcripts_root()))


def close_stale(live: set[str], audit: "HostAudit") -> None:
    """Close the copies of sessions that ended while no host controller ran
    (their bottles are gone): the last checkpoint is final."""
    root = transcripts_root()
    for follow in sorted(root.glob(f"*/*/{_FOLLOW}")) if root.is_dir() else []:
        session, activation = follow.parent.parent.name, follow.parent.name
        if session in live:
            continue
        follower = TranscriptFollower.resume(session=session, activation=activation, audit=audit)
        if follower is not None:
            follower.close()


__all__ = ["TranscriptFollower", "close_stale", "copy_dir", "parse_listing", "poll_script",
           "shell_glob", "transcripts_root"]
