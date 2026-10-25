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
from .gateway import AGENT_SUBNET_V6, GATEWAY_NETWORK


def _service_running() -> bool:
    return _container.service_running()


def _summary(subnet: str) -> str:
    """What installing the firewall changes on this Mac, and how to undo it."""
    return (
        "The macOS backend needs a one-time privileged step. It changes, system-wide:\n"
        f"  - {host_firewall.RULES_PATH}: pf rules that drop traffic from the agent\n"
        f"    network ({subnet}, {AGENT_SUBNET_V6}) to this Mac and to anything but\n"
        "    the gateway. Nothing else on this Mac matches them.\n"
        f"  - {host_firewall.DAEMON_PATH}: a root job that loads them at boot and\n"
        "    every minute, turns pf on if it's off (/etc/pf.conf isn't edited), and\n"
        "    turns IPv6 off on the agent network's bridge only.\n"
        "  Undo it with: bot-bottle backend teardown --backend=macos-container\n\n"
    )


def _choose() -> str | None:
    """The agent subnet to install; None (reported) when every candidate is
    taken."""
    try:
        subnet, rejected = host_firewall.choose_subnet()
    except RuntimeError as e:
        sys.stderr.write(f"macos-container backend: {e}. Free one of them, or use "
                         "another backend.\n")
        return None
    current = host_firewall.installed_subnet()
    if current and subnet != current:
        sys.stderr.write(
            f"The agent network's {current} is also used on this Mac "
            f"({'; '.join(rejected)}), so agents move to {subnet}. With no bottles "
            f"running, `container network rm {GATEWAY_NETWORK}` once this is "
            "installed; the next launch recreates it.\n\n")
    return subnet


def _firewall() -> int:
    """Pick the agent subnet and install the firewall for it, unless it's
    already in place."""
    subnet = _choose()
    if subnet is None:
        return 1
    if os.geteuid() != 0 and host_firewall.state(subnet, AGENT_SUBNET_V6) == "ok":
        sys.stderr.write("macos-container backend: ready.\n")
        return 0
    return _install_firewall(subnet)


def _install_firewall(subnet: str) -> int:
    """Install the host firewall for `subnet`: directly as root, else print
    the sudo block."""
    if os.geteuid() == 0:
        host_firewall.install(subnet, AGENT_SUBNET_V6)
        sys.stderr.write(
            f"Installed the host firewall ({host_firewall.DAEMON_LABEL}): agents "
            f"on {subnet} / {AGENT_SUBNET_V6} can't reach this Mac or "
            "anything but the gateway.\n"
        )
        return 0
    sys.stderr.write(_summary(subnet) + "Install it (one copy-paste; needs your password):\n\n")
    sys.stdout.write(host_firewall.install_commands(subnet, AGENT_SUBNET_V6))
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
        return _firewall()
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
    return _firewall()


def teardown() -> int:
    if os.geteuid() == 0:
        host_firewall.uninstall(host_firewall.agent_subnet())
        sys.stderr.write(f"Removed the host firewall ({host_firewall.DAEMON_LABEL}).\n")
    else:
        sys.stderr.write("Remove the host firewall (one copy-paste):\n\n")
        sys.stdout.write(host_firewall.uninstall_commands(host_firewall.agent_subnet()))
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
    subnet = host_firewall.agent_subnet()
    firewall = host_firewall.state(subnet, AGENT_SUBNET_V6)
    sys.stderr.write({
        "ok": "host firewall: installed\n",
        "missing": "host firewall: NOT installed\n",
        "stale": "host firewall: installed but out of date\n",
    }[firewall])
    if firewall != "ok":
        ok = False
    sys.stderr.write(f"agent network: {subnet}\n")
    for clash in host_firewall.conflicts(subnet):
        sys.stderr.write(f"  also used on this Mac: {clash}\n")
        ok = False
    if not ok:
        sys.stderr.write("\nRun: bot-bottle backend setup --backend=macos-container\n")
    return 0 if ok else 1
