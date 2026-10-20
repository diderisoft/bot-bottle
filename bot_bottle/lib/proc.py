"""Run a child process whose output belongs to the current output sink."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

from . import log


def run_streaming(argv: Sequence[str], **kwargs: Any) -> int:
    """Run `argv` and return its exit code. Without a redirected sink its
    output goes straight to this process's stdout/stderr; with one (a host
    controller job), each line of stdout and stderr goes to the sink."""
    if not log.redirected():
        return subprocess.run(list(argv), check=False, **kwargs).returncode
    with subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", **kwargs,
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write_line(line.rstrip("\n"))
        return proc.wait()


__all__ = ["run_streaming"]
