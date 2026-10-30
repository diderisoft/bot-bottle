"""Run a child process whose output goes to the current output sink."""

from __future__ import annotations

import re
import subprocess
from collections import deque
from collections.abc import Sequence
from typing import Any

from . import log

# Output lines a failed command's CalledProcessError keeps, for its reason.
_TAIL = 60
# apt's own errors, under BuildKit's `#10 3.908 ` step/time prefix.
_APT_ERROR = re.compile(r"^(?:#\d+ )?(?:[\d.]+ )?(E: .+)$")


def run_streaming(argv: Sequence[str], *, check: bool = False, **kwargs: Any) -> int:
    """Run `argv` and return its exit code. Output goes to this process's
    stdio, or line by line to a redirected sink (a host-controller job).
    `check=True` raises `CalledProcessError` on non-zero exit, its `output`
    the last lines when they were redirected."""
    tail: deque[str] = deque(maxlen=_TAIL)
    if not log.redirected():
        code = subprocess.run(list(argv), check=False, **kwargs).returncode
    else:
        with subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", **kwargs,
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                log.write_line(line)
                tail.append(line)
            code = proc.wait()
    if check and code != 0:
        raise subprocess.CalledProcessError(code, list(argv), output="\n".join(tail))
    return code


def build_failure(ref: str, e: subprocess.CalledProcessError) -> str:
    """One line saying why building `ref` failed: apt's first `E:` line, else
    the last ERROR line, else a pointer to the build output."""
    lines = [line.strip() for line in (e.output or "").splitlines() if line.strip()]
    apt = [m.group(1) for line in lines if (m := _APT_ERROR.match(line))]
    errors = [line for line in lines if "ERROR" in line or line.startswith("Error:")]
    reason = (apt or errors[-1:] or [""])[0][:300]
    if not reason:
        return f"building {ref} failed (exit {e.returncode}); see its build output"
    return f"building {ref} failed: {reason}"


__all__ = ["build_failure", "run_streaming"]
