"""The host pf anchor that keeps macOS agents off the host.

Apple's `--internal` network is vmnet host-only: no NAT to the internet, but
guests still reach the host's own addresses (its LAN address, the vmnet
gateway), and with IP forwarding on, anything that NATs the subnet would let
them out. The anchor drops traffic from the agent subnets to the host itself
and to anything off the subnet. Agent-to-gateway traffic stays on the subnet
and passes.

It lives under the `com.apple/*` hook already in `/etc/pf.conf`, so that file
is never edited. A root LaunchDaemon loads it at boot and re-applies it every
minute, in case something reloads pf. Installing it needs root once; the
launch-time isolation probe proves it's in force.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

ANCHOR = "com.apple/io.bot-bottle"
RULES_PATH = Path("/etc/pf.anchors/io.bot-bottle")
DAEMON_LABEL = "io.bot-bottle.pf"
DAEMON_PATH = Path("/Library/LaunchDaemons") / f"{DAEMON_LABEL}.plist"
# Re-apply this often (seconds): a pf reload elsewhere can drop the anchor.
REAPPLY_SECONDS = 60


def render_rules(subnet: str, subnet_v6: str) -> str:
    """The anchor's rules for the agent subnets."""
    return (
        "# Installed by `bot-bottle backend setup --backend=macos-container`.\n"
        "# Agents may reach the gateway on their own subnet and nothing else:\n"
        "# not the host's addresses, not the LAN, not the internet.\n"
        f"block drop in quick inet from {subnet} to (self)\n"
        f"block drop in quick inet from {subnet} to ! {subnet}\n"
        f"block drop in quick inet6 from {subnet_v6} to (self)\n"
        f"block drop in quick inet6 from {subnet_v6} to ! {subnet_v6}\n"
    )


def _apply_script() -> str:
    # Enable pf only when it's off: each `pfctl -E` takes another reference.
    return (
        "/sbin/pfctl -s info 2>/dev/null | /usr/bin/grep -q '^Status: Enabled' "
        "|| /sbin/pfctl -E; "
        f"/sbin/pfctl -a {ANCHOR} -f {RULES_PATH}"
    )


def render_daemon() -> bytes:
    """The LaunchDaemon plist that loads the anchor at boot and re-applies it."""
    return plistlib.dumps({
        "Label": DAEMON_LABEL,
        "ProgramArguments": ["/bin/sh", "-c", _apply_script()],
        "RunAtLoad": True,
        "StartInterval": REAPPLY_SECONDS,
        "StandardErrorPath": f"/var/log/{DAEMON_LABEL}.log",
    })


def state(subnet: str, subnet_v6: str) -> str:
    """"ok", "missing" or "stale", from the installed files (both are
    world-readable, so no root is needed). Whether pf has the rules loaded
    needs root to see; the launch-time probe checks that."""
    try:
        rules = RULES_PATH.read_text(encoding="utf-8")
        daemon = DAEMON_PATH.read_bytes()
    except OSError:
        return "missing"
    if rules != render_rules(subnet, subnet_v6) or daemon != render_daemon():
        return "stale"
    return "ok"


def install(subnet: str, subnet_v6: str) -> None:
    """Write the rules and daemon and (re)load the daemon. Needs root."""
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULES_PATH.write_text(render_rules(subnet, subnet_v6), encoding="utf-8")
    RULES_PATH.chmod(0o644)
    DAEMON_PATH.write_bytes(render_daemon())
    DAEMON_PATH.chmod(0o644)
    subprocess.run(["launchctl", "bootout", f"system/{DAEMON_LABEL}"],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", "system", str(DAEMON_PATH)], check=True)


def uninstall() -> None:
    """Unload the daemon, flush the anchor and remove both files. Needs root."""
    subprocess.run(["launchctl", "bootout", f"system/{DAEMON_LABEL}"],
                   capture_output=True, check=False)
    subprocess.run(["/sbin/pfctl", "-a", ANCHOR, "-F", "all"],
                   capture_output=True, check=False)
    DAEMON_PATH.unlink(missing_ok=True)
    RULES_PATH.unlink(missing_ok=True)


def install_commands(subnet: str, subnet_v6: str) -> str:
    """The same install as a copy-paste block for a non-root shell."""
    return (
        f"sudo tee {RULES_PATH} >/dev/null <<'RULES'\n"
        f"{render_rules(subnet, subnet_v6)}"
        "RULES\n"
        f"sudo tee {DAEMON_PATH} >/dev/null <<'PLIST'\n"
        f"{render_daemon().decode()}"
        "PLIST\n"
        f"sudo launchctl bootout system/{DAEMON_LABEL} 2>/dev/null || true\n"
        f"sudo launchctl bootstrap system {DAEMON_PATH}\n"
    )


def uninstall_commands() -> str:
    """The same uninstall as a copy-paste block for a non-root shell."""
    return (
        f"sudo launchctl bootout system/{DAEMON_LABEL} 2>/dev/null || true\n"
        f"sudo pfctl -a {ANCHOR} -F all\n"
        f"sudo rm -f {DAEMON_PATH} {RULES_PATH}\n"
    )


__all__ = [
    "ANCHOR", "DAEMON_LABEL", "DAEMON_PATH", "RULES_PATH",
    "install", "install_commands", "render_daemon", "render_rules", "state",
    "uninstall", "uninstall_commands",
]
