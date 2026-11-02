"""Image files from a running bottle's workspace, for the console to show: an
agent makes a picture as a file and mentions its path (`workspace_file`).

The file is read inside the bottle through `Bottle.exec`, never from a host
path. Only absolute, normalized paths under `/home/node` or `/tmp` with an
image extension are read; the guest resolves symlinks and the resolved path
is checked again, there and here. The content type comes from the extension,
never from the guest, and a file over `MAX_BYTES` is refused."""

from __future__ import annotations

import base64
import binascii
import posixpath
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...backend.base import Bottle

# The largest file sent to the console (the console caps it the same).
MAX_BYTES = 10 * 1024 * 1024
MAX_PATH_CHARS = 4096
ROOTS = ("/home/node", "/tmp")
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
# A read of up to MAX_BYTES gives up after this long.
READ_TIMEOUT_SECONDS = 30.0


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


__all__ = ["CONTENT_TYPES", "MAX_BYTES", "ROOTS", "WorkspaceFileError", "content_type",
           "read_image", "read_script"]
