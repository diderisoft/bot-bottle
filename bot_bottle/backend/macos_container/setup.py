"""macOS backend setup and status. No privileged setup: it needs Apple's
`container` CLI and its system service running.
"""

from __future__ import annotations

import shutil
import sys

from . import util as _container


def _service_running() -> bool:
    return _container.service_running()


def setup() -> int:
    if not _container.is_macos():
        sys.stderr.write("macos-container backend requires macOS.\n")
        return 1
    if shutil.which("container") is None:
        sys.stderr.write("Apple Container is required but was not found on PATH.\n")
        sys.stderr.write("Install: https://github.com/apple/container/releases\n")
        return 1
    if not _service_running():
        sys.stderr.write(
            "Apple Container is installed but its system service isn't "
            "running. Start it with: container system start\n"
        )
        return 1
    sys.stderr.write(
        "macos-container backend: ready — no privileged host setup required.\n"
    )
    return 0


def teardown() -> int:
    sys.stderr.write(
        "macos-container backend: nothing to undo — it provisions no "
        "privileged host state. The Apple Container CLI and its system "
        "service are left as-is (stop the service yourself with "
        "`container system stop` if you want).\n"
    )
    return 0


def status() -> int:
    ok = True
    if _container.is_macos():
        sys.stderr.write("host: macOS\n")
    else:
        sys.stderr.write("host: NOT macOS (backend unsupported here)\n")
        ok = False
    if shutil.which("container") is not None:
        sys.stderr.write("container CLI on PATH: yes\n")
    else:
        sys.stderr.write("container CLI on PATH: NO\n")
        ok = False
    if ok:
        sys.stderr.write(
            f"container system service: {'running' if _service_running() else 'NOT running'}\n"
        )
        if not _service_running():
            ok = False
    if not ok:
        sys.stderr.write("\nRun: bot-bottle backend setup --backend=macos-container\n")
    return 0 if ok else 1
