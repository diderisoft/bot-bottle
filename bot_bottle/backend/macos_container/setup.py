"""macOS backend setup and status. It needs Apple's `container` CLI and its
system service running, plus a one-time privileged step: the host pf anchor
that keeps agents off the host (`host_firewall`).
"""

from __future__ import annotations

import os
import shutil
import sys

from . import host_firewall
from . import util as _container
from .gateway import AGENT_SUBNET, AGENT_SUBNET_V6


def _service_running() -> bool:
    return _container.service_running()


def _install_firewall() -> int:
    """Install the host firewall: directly as root, else print the sudo
    block."""
    if os.geteuid() == 0:
        host_firewall.install(AGENT_SUBNET, AGENT_SUBNET_V6)
        sys.stderr.write(
            f"Installed the host firewall ({host_firewall.DAEMON_LABEL}): agents "
            f"on {AGENT_SUBNET} / {AGENT_SUBNET_V6} can't reach this Mac or "
            "anything but the gateway.\n"
        )
        return 0
    sys.stderr.write(
        "Host firewall: agents on the host-only network can otherwise reach "
        "this Mac's own addresses. Install the pf anchor that blocks that "
        "(needs root once; it persists across reboots):\n\n"
    )
    sys.stdout.write(host_firewall.install_commands(AGENT_SUBNET, AGENT_SUBNET_V6))
    sys.stderr.write(
        "\n(Or re-run this as root to install it directly: "
        "sudo bot-bottle backend setup --backend=macos-container)\n"
    )
    return 0


def setup() -> int:
    if not _container.is_macos():
        sys.stderr.write("macos-container backend requires macOS.\n")
        return 1
    if os.geteuid() == 0:
        # The container service is per-user; as root only the firewall applies.
        return _install_firewall()
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
    if host_firewall.state(AGENT_SUBNET, AGENT_SUBNET_V6) == "ok":
        sys.stderr.write("macos-container backend: ready.\n")
        return 0
    return _install_firewall()


def teardown() -> int:
    if os.geteuid() == 0:
        host_firewall.uninstall()
        sys.stderr.write(f"Removed the host firewall ({host_firewall.DAEMON_LABEL}).\n")
    else:
        sys.stderr.write("Remove the host firewall (one copy-paste):\n\n")
        sys.stdout.write(host_firewall.uninstall_commands())
    sys.stderr.write(
        "The Apple Container CLI and its system service are left as-is (stop "
        "the service yourself with `container system stop` if you want).\n"
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
    firewall = host_firewall.state(AGENT_SUBNET, AGENT_SUBNET_V6)
    sys.stderr.write({
        "ok": "host firewall: installed\n",
        "missing": "host firewall: NOT installed\n",
        "stale": "host firewall: installed but out of date\n",
    }[firewall])
    if firewall != "ok":
        ok = False
    if not ok:
        sys.stderr.write("\nRun: bot-bottle backend setup --backend=macos-container\n")
    return 0 if ok else 1
