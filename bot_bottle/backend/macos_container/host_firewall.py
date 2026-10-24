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

import ipaddress
import plistlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

ANCHOR = "com.apple/io.bot-bottle"
RULES_PATH = Path("/etc/pf.anchors/io.bot-bottle")
DAEMON_LABEL = "io.bot-bottle.pf"
DAEMON_PATH = Path("/Library/LaunchDaemons") / f"{DAEMON_LABEL}.plist"
# Re-apply this often (seconds): a pf reload elsewhere can drop the anchor.
REAPPLY_SECONDS = 60


def host_address(subnet: str) -> str:
    """The host's own address on a vmnet subnet: its first address, which is
    the agents' default route and resolver."""
    return str(ipaddress.ip_network(subnet)[1])


def render_rules(subnet: str, subnet_v6: str) -> str:
    """The anchor's rules for the agent subnets. The host's IPv4 address on the
    subnet is named outright: `(self)` doesn't match the vmnet bridge's own
    address, and that address is on the subnet, so `! subnet` misses it too.
    The host's IPv6 addresses on the bridge (link-local, and an autoconf one
    on the ULA prefix) can't be named, so the daemon turns IPv6 off on the
    bridge instead (#720)."""
    return (
        "# Installed by `bot-bottle backend setup --backend=macos-container`.\n"
        "# Agents may reach the gateway on their own subnet and nothing else:\n"
        "# not the host's addresses, not the LAN, not the internet.\n"
        f"block drop in quick inet from {subnet} to {host_address(subnet)}\n"
        f"block drop in quick inet from {subnet} to (self)\n"
        f"block drop in quick inet from {subnet} to ! {subnet}\n"
        f"block drop in quick inet6 from {subnet_v6} to (self)\n"
        f"block drop in quick inet6 from {subnet_v6} to ! {subnet_v6}\n"
    )


def _bridge_lookup(subnet: str) -> str:
    """Shell that prints the bridge holding the host's address on `subnet`.
    Its name changes whenever the agent network is recreated."""
    address = host_address(subnet).replace(".", r"\.")
    return ("/sbin/ifconfig | /usr/bin/awk "
            f"'/^[a-z]/{{i=$1}} /inet {address} /{{sub(\":\",\"\",i); print i}}'")


def _apply_script(subnet: str) -> str:
    # Enable pf only when it's off: each `pfctl -E` takes another reference.
    # Nothing on the agent network uses IPv6, so the host stops speaking it
    # there; the agent can't turn it back on.
    return (
        "/sbin/pfctl -s info 2>/dev/null | /usr/bin/grep -q '^Status: Enabled' "
        "|| /sbin/pfctl -E; "
        f"/sbin/pfctl -a {ANCHOR} -f {RULES_PATH}; "
        f"br=$({_bridge_lookup(subnet)}); "
        '[ -z "$br" ] || /sbin/ifconfig "$br" inet6 ifdisabled'
    )


def render_daemon(subnet: str) -> bytes:
    """The LaunchDaemon plist that loads the anchor at boot and re-applies it,
    with IPv6 off on the agent bridge."""
    return plistlib.dumps({
        "Label": DAEMON_LABEL,
        "ProgramArguments": ["/bin/sh", "-c", _apply_script(subnet)],
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
    if rules != render_rules(subnet, subnet_v6) or daemon != render_daemon(subnet):
        return "stale"
    return "ok"


def install(subnet: str, subnet_v6: str) -> None:
    """Write the rules and daemon and (re)load the daemon. Needs root."""
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULES_PATH.write_text(render_rules(subnet, subnet_v6), encoding="utf-8")
    RULES_PATH.chmod(0o644)
    DAEMON_PATH.write_bytes(render_daemon(subnet))
    DAEMON_PATH.chmod(0o644)
    subprocess.run(["launchctl", "bootout", f"system/{DAEMON_LABEL}"],
                   capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", "system", str(DAEMON_PATH)], check=True)


def uninstall(subnet: str) -> None:
    """Unload the daemon, flush the anchor, turn IPv6 back on on the agent
    bridge and remove both files. Needs root."""
    subprocess.run(["launchctl", "bootout", f"system/{DAEMON_LABEL}"],
                   capture_output=True, check=False)
    bridge = agent_bridge(subnet)
    if bridge:
        subprocess.run(["/sbin/ifconfig", bridge, "inet6", "-ifdisabled"],
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
        f"{render_daemon(subnet).decode()}"
        "PLIST\n"
        f"sudo launchctl bootout system/{DAEMON_LABEL} 2>/dev/null || true\n"
        f"sudo launchctl bootstrap system {DAEMON_PATH}\n"
    )


def uninstall_commands(subnet: str) -> str:
    """The same uninstall as a copy-paste block for a non-root shell."""
    return (
        f"sudo launchctl bootout system/{DAEMON_LABEL} 2>/dev/null || true\n"
        f"sudo pfctl -a {ANCHOR} -F all\n"
        f'br=$({_bridge_lookup(subnet)}); [ -z "$br" ] || sudo ifconfig "$br" inet6 -ifdisabled\n'
        f"sudo rm -f {DAEMON_PATH} {RULES_PATH}\n"
    )


def agent_bridge(subnet: str) -> str | None:
    """The bridge interface holding the host's address on `subnet`, or None
    when there's none (the network doesn't exist yet)."""
    out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True,
                         check=False).stdout
    iface, wanted = None, f"inet {host_address(subnet)} "
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":", 1)[0]
        elif wanted in line:
            return iface
    return None


def ipv6_off(bridge: str) -> bool:
    """Whether the host has IPv6 off on `bridge` (`IFDISABLED` in its nd6
    options), readable without root."""
    out = subprocess.run(["/sbin/ifconfig", bridge], capture_output=True, text=True,
                         check=False).stdout
    return any("nd6 options=" in line and "IFDISABLED" in line for line in out.splitlines())


def wait_ipv6_off(subnet: str, *, timeout: float = REAPPLY_SECONDS + 15,
                  poll: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> bool:
    """Wait for the daemon to turn IPv6 off on the agent bridge: a recreated
    network gets a new bridge, which the daemon reaches within a minute."""
    deadline = clock() + timeout
    while True:
        bridge = agent_bridge(subnet)
        if bridge and ipv6_off(bridge):
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


__all__ = [
    "ANCHOR", "DAEMON_LABEL", "DAEMON_PATH", "RULES_PATH",
    "agent_bridge", "host_address", "install", "install_commands", "ipv6_off",
    "render_daemon", "render_rules", "state", "wait_ipv6_off",
    "uninstall", "uninstall_commands",
]
