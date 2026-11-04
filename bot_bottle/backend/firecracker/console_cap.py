"""Write a microVM's serial console to a capped, owner-only log.

Firecracker writes the guest's serial console to its stdout without limit; a
chatty guest (the gateway at `log: 2`, before #934) filled hundreds of MB. The
VMM's stdout is piped here instead: `console.log` is created 0600, and once it
passes `max_bytes` it becomes `console.log.1` (replacing the previous one) and a
fresh `console.log` starts, so at most twice the cap is kept and the newest
output is always in `console.log`.

  python3 -m bot_bottle.backend.firecracker.console_cap <path> [max_bytes]
"""

from __future__ import annotations

import io
import os
import sys

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
_CHUNK = 64 * 1024


def _open(path: str) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)  # an older, wider log left at this path
    return fd


def pump(source: io.BufferedIOBase, path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """Copy `source` to `path` until EOF, rotating at `max_bytes`."""
    fd = _open(path)
    size = os.fstat(fd).st_size
    try:
        while True:
            chunk = source.read1(_CHUNK)  # what the pipe has, without waiting to fill
            if not chunk:
                return
            if size and size + len(chunk) > max_bytes:
                os.close(fd)
                os.replace(path, f"{path}.1")
                fd, size = _open(path), 0
            os.write(fd, chunk)
            size += len(chunk)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        sys.stderr.write("usage: console_cap <path> [max_bytes]\n")
        return 2
    max_bytes = int(args[1]) if len(args) > 1 else DEFAULT_MAX_BYTES
    stdin = sys.stdin.buffer
    assert isinstance(stdin, io.BufferedIOBase)
    pump(stdin, args[0], max_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
