"""Workspace file operations for the console: reading image files an agent made,
and writing files the operator uploads from the console.

**Read** (`workspace_file`): the agent makes a picture and mentions its path.
Only absolute, normalized paths under `/home/node` or `/tmp` with an image
extension are read; the guest resolves symlinks and the resolved path is
checked again, there and here. The content type comes from the extension,
never from the guest, and a file over `MAX_BYTES` is refused.

**Write** (`put_workspace_file`): the operator uploads a file from the console.
Only absolute, normalized paths under the upload root (`/home/node/uploads`)
are written; the guest checks the prefix too. Files over `WRITE_MAX_BYTES` are
refused. The file is written via `Bottle.exec`, never from a host path."""

from __future__ import annotations

import base64
import binascii
import posixpath
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from ...backend.base import Bottle

# The largest file sent to the console (the console caps it the same).
MAX_BYTES = 10 * 1024 * 1024
MAX_PATH_CHARS = 4096
ROOTS = ("/home/node", "/tmp")
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
# A read of up to MAX_BYTES gives up after this long.
READ_TIMEOUT_SECONDS = 30.0

# Uploads from the console land here; all files are accepted (any extension).
UPLOAD_ROOT = "/home/node/uploads"
# The largest file the console may upload (5 MiB).
WRITE_MAX_BYTES = 5 * 1024 * 1024
WRITE_TIMEOUT_SECONDS = 30.0


class WorkspaceFileError(Exception):
    """Why a file can't be shown; the message goes to the console."""


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path.startswith(root.rstrip("/") + "/") for root in roots)


def content_type(path: str, roots: tuple[str, ...] = ROOTS) -> str:
    """The content type to serve `path` as, or WorkspaceFileError if it isn't
    an image path the console may ask for."""
    if not path or len(path) > MAX_PATH_CHARS or any(c in path for c in "\0\n\r"):
        raise WorkspaceFileError("that isn't a file path")
    if not path.startswith("/") or posixpath.normpath(path) != path:
        raise WorkspaceFileError("the path must be absolute and normalized")
    if not _under(path, roots):
        raise WorkspaceFileError(f"only files under {' or '.join(roots)} can be shown")
    kind = CONTENT_TYPES.get(posixpath.splitext(path)[1].lower())
    if kind is None:
        raise WorkspaceFileError("only png, jpg, gif, webp and svg images can be shown")
    return kind


def read_script(path: str, roots: tuple[str, ...] = ROOTS, limit: int = MAX_BYTES) -> str:
    """The `exec` script: `R <resolved path>`, then `E <reason>` or
    `D <base64 of at most limit + 1 bytes>`."""
    allowed = "|".join(f"{shlex.quote(root.rstrip('/'))}/*" for root in roots)
    return "\n".join([
        f"p={shlex.quote(path)}",
        'r=$(readlink -f -- "$p") || r=',
        'printf "R %s\\n" "$r"',
        'case "$r" in',
        f"  {allowed}) ;;",
        '  *) echo "E outside"; exit 0;;',
        "esac",
        '[ -f "$r" ] || { echo "E missing"; exit 0; }',
        '[ -r "$r" ] || { echo "E unreadable"; exit 0; }',
        'printf "D "',
        f'head -c {limit + 1} -- "$r" | base64 | tr -d "\\n"',
        'printf "\\n"',
    ])


_REASONS = {"outside": "that file links outside {roots}",
            "missing": "there's no such file in the bottle",
            "unreadable": "the agent can't read that file"}


def read_image(bottle: "Bottle", path: str, roots: tuple[str, ...] = ROOTS,
               limit: int = MAX_BYTES) -> dict[str, object]:
    """Read the image at `path` in `bottle`: `content_type`, `data` (base64)
    and `size`. WorkspaceFileError when it can't be shown."""
    kind = content_type(path, roots)
    r = bottle.exec(read_script(path, roots, limit), timeout=READ_TIMEOUT_SECONDS)
    if r.returncode != 0:
        raise WorkspaceFileError("couldn't read the file in the bottle")
    resolved, reason, encoded = "", "", None
    for line in r.stdout.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "R":
            resolved = rest
        elif tag == "E":
            reason = rest
        elif tag == "D":
            encoded = rest
    if reason:
        raise WorkspaceFileError(_REASONS.get(reason, "couldn't read the file in the bottle")
                                 .format(roots=" or ".join(roots)))
    # The guest checked too, but it's the guest.
    if not _under(resolved, roots):
        raise WorkspaceFileError(_REASONS["outside"].format(roots=" or ".join(roots)))
    if posixpath.splitext(resolved)[1].lower() not in CONTENT_TYPES:
        raise WorkspaceFileError("that file links to something that isn't an image")
    if encoded is None:
        raise WorkspaceFileError("couldn't read the file in the bottle")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise WorkspaceFileError("couldn't read the file in the bottle") from e
    if len(data) > limit:
        raise WorkspaceFileError(f"the file is over {limit // (1024 * 1024)} MiB")
    return {"content_type": kind, "data": base64.b64encode(data).decode("ascii"),
            "size": len(data)}


def _safe_upload_path(path: str, upload_root: str = UPLOAD_ROOT) -> None:
    """Validate `path` as a write destination, or raise WorkspaceFileError."""
    root = upload_root.rstrip("/")
    if not path or len(path) > MAX_PATH_CHARS or any(c in path for c in "\0\n\r"):
        raise WorkspaceFileError("that isn't a file path")
    if not path.startswith("/") or posixpath.normpath(path) != path:
        raise WorkspaceFileError("the path must be absolute and normalized")
    if not path.startswith(root + "/"):
        raise WorkspaceFileError(f"uploads must go under {root}")
    if posixpath.basename(path) in (".", ".."):
        raise WorkspaceFileError("the filename is invalid")


def write_script(path: str, data_b64: str, upload_root: str = UPLOAD_ROOT) -> str:
    """The `exec` script to write base64-encoded data to `path` inside the
    bottle. Outputs 'OK' on success or 'E <reason>' on failure."""
    root = upload_root.rstrip("/")
    # data_b64 uses only A-Za-z0-9+/= — safe inside single quotes.
    return "\n".join([
        f"p={shlex.quote(path)}",
        'case "$p" in',
        f"  {shlex.quote(root)}/*) ;;",
        '  *) echo "E outside"; exit 0;;',
        "esac",
        'dir=$(dirname "$p")',
        'mkdir -p "$dir" 2>/dev/null || { echo "E mkdir"; exit 0; }',
        f"printf '%s' {shlex.quote(data_b64)} | base64 -d > \"$p\" && echo 'OK' || echo 'E write'",
    ])


def write_file(bottle: "Bottle", path: str, data: bytes,
               upload_root: str = UPLOAD_ROOT) -> dict[str, object]:
    """Write `data` to `path` in `bottle`'s workspace: `{'path', 'size'}`.
    WorkspaceFileError when it can't be written."""
    _safe_upload_path(path, upload_root)
    if len(data) > WRITE_MAX_BYTES:
        raise WorkspaceFileError(
            f"the file is over {WRITE_MAX_BYTES // (1024 * 1024)} MiB")
    data_b64 = base64.b64encode(data).decode("ascii")
    r = bottle.exec(write_script(path, data_b64, upload_root),
                    timeout=WRITE_TIMEOUT_SECONDS)
    if r.returncode != 0:
        raise WorkspaceFileError("couldn't write the file in the bottle")
    _WRITE_ERRORS = {
        "outside": f"that path is outside {upload_root}",
        "mkdir": "couldn't create the upload directory in the bottle",
        "write": "couldn't write the file in the bottle",
    }
    for line in r.stdout.splitlines():
        if line == "OK":
            return {"path": path, "size": len(data)}
        if line.startswith("E "):
            reason = line[2:]
            raise WorkspaceFileError(
                _WRITE_ERRORS.get(reason, "couldn't write the file in the bottle"))
    raise WorkspaceFileError("couldn't write the file in the bottle")


def serve_read(attach: "Callable[[str], Any]", identity: str, path: str) -> dict[str, object]:
    """`BottleService.read_workspace_image`: the image at `path` in the bottle
    `attach(identity)` reaches (`content_type`, `data`, `size`), or `error`.
    A bad path is refused before reaching the bottle."""
    try:
        content_type(path)
        attached = attach(identity)
        if isinstance(attached, str):
            return {"error": attached}
        return read_image(attached[0], path)
    except WorkspaceFileError as e:
        return {"error": str(e)}


def serve_write(attach: "Callable[[str], Any]", identity: str, path: str,
                data: bytes) -> dict[str, object]:
    """`BottleService.write_workspace_file`: write `data` to `path` in the bottle
    `attach(identity)` reaches (`path`, `size`), or `error`. A bad path is
    refused before reaching the bottle."""
    try:
        _safe_upload_path(path)
        attached = attach(identity)
        if isinstance(attached, str):
            return {"error": attached}
        return write_file(attached[0], path, data)
    except WorkspaceFileError as e:
        return {"error": str(e)}


__all__ = ["CONTENT_TYPES", "MAX_BYTES", "ROOTS", "UPLOAD_ROOT", "WRITE_MAX_BYTES",
           "WorkspaceFileError", "content_type", "read_image", "read_script", "serve_read",
           "serve_write", "write_file", "write_script"]
