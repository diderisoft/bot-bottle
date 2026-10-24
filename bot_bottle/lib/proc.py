"""Run a child process whose output goes to the current output sink."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

from . import log


def run_streaming(argv: Sequence[str], *, check: bool = False, **kwargs: Any) -> int:
    """Run `argv` and return its exit code. Output goes to this process's
    stdio, or line by line to a redirected sink (a host-controller job).
    `check=True` raises `CalledProcessError` on non-zero exit."""
    if not log.redirected():
        code = subprocess.run(list(argv), check=False, **kwargs).returncode
    else:
        with subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", **kwargs,
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write_line(line.rstrip("\n"))
            code = proc.wait()
    if check and code != 0:
        raise subprocess.CalledProcessError(code, list(argv))
    return code


__all__ = ["run_streaming"]
