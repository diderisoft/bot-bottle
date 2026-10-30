"""The host pf anchor that keeps macOS agents off the host.

Apple's `--internal` network is vmnet host-only: no NAT to the internet, but
guests still reach the host's own addresses (its LAN address, the vmnet
gateway), and with IP forwarding on, anything that NATs the subnet would let
them out. The anchor drops traffic from the agent subnets to the host itself
and to anything off the subnet. Agent-to-gateway traffic stays on the subnet
and passes. Traffic between agents is switched on the vmnet bridge and never
reaches pf; each agent's own firewall stops it (`agent_firewall`).

It lives under the `com.apple/*` hook already in `/etc/pf.conf`, so that file
is never edited. A root LaunchDaemon loads it at boot and re-applies it every
minute, in case something reloads pf. Installing it needs root once; the
launch-time isolation probe proves it's in force.

The installed rules are where the agent subnet is recorded: setup picks a
range no LAN, VPN or other interface on this Mac uses, and the network and
launch read it back (`agent_subnet`).
"""

from __future__ import annotations

import ipaddress
import plistlib
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from ...environment import (
    LIVE_REVERSE_DNS, macos_agent_subnet_v6, macos_agent_subnet_v6_candidates, reverse_dns,
)

# One anchor per environment: `io.bot-bottle` for live, `io.bot-bottle.<name>`
# for a named one (each installed by its own `backend setup`).
_RULES_DIR = Path("/etc/pf.anchors")
ANCHOR = f"com.apple/{reverse_dns()}"
RULES_PATH = _RULES_DIR / reverse_dns()
DAEMON_LABEL = f"{reverse_dns()}.pf"
DAEMON_PATH = Path("/Library/LaunchDaemons") / f"{DAEMON_LABEL}.plist"
# Re-apply this often (seconds): a pf reload elsewhere can drop the anchor.
REAPPLY_SECONDS = 60
# Agent subnets setup tries, in order, skipping any this Mac already uses.
CANDIDATE_SUBNETS = ("192.168.128.0/24", "192.168.200.0/24", "10.213.128.0/24",
                     "172.30.128.0/24")


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
    Its name changes whenever the agent network is recreated. Only `bridge*`
    interfaces count, so a LAN that happens to use the address is never
    touched."""
    address = host_address(subnet).replace(".", r"\.")
    return ("/sbin/ifconfig | /usr/bin/awk "
            "'/^[a-z]/{i=\"\"} /^bridge[0-9]+:/{i=$1} "
            f"i!=\"\" && /inet {address} /{{sub(\":\",\"\",i); print i}}'")


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
    wanted = host_address(subnet)
    for iface, address, _ in _interface_addresses():
        if iface.startswith("bridge") and address == wanted:
            return iface
    return None


def _interface_addresses() -> list[tuple[str, str, int]]:
    """(interface, IPv4 address, prefix length) for every address on this Mac."""
    out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True,
                         check=False).stdout
    found: list[tuple[str, str, int]] = []
    iface = ""
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":", 1)[0]
            continue
        words = line.split()
        if len(words) >= 4 and words[0] == "inet" and words[2] == "netmask":
            try:
                prefix = bin(int(words[3], 16)).count("1")
            except ValueError:
                continue
            found.append((iface, words[1], prefix))
    return found


def _route_network(destination: str) -> ipaddress.IPv4Network | None:
    """`netstat -rn` destination as a network: `192.168.1` is a /24, `127` a
    /8, `100.64/10` explicit, a full address a host route."""
    if destination == "default":
        return None
    address, _, length = destination.partition("/")
    octets = address.split(".")
    if not all(o.isdigit() for o in octets) or len(octets) > 4:
        return None
    prefix = int(length) if length.isdigit() else 8 * len(octets)
    try:
        return ipaddress.IPv4Network(
            ".".join(octets + ["0"] * (4 - len(octets))) + f"/{prefix}", strict=False)
    except ValueError:
        return None


def conflicts(subnet: str) -> list[str]:
    """What else on this Mac uses `subnet`: interface addresses (other than
    the agent bridge's own) and routes via anything but a vmnet bridge. Each
    is a line naming the interface."""
    net = ipaddress.ip_network(subnet)
    ours = host_address(subnet)
    found = []
    for iface, address, prefix in _interface_addresses():
        if iface.startswith("bridge") and address == ours:
            continue
        if ipaddress.ip_network(f"{address}/{prefix}", strict=False).overlaps(net):
            found.append(f"{iface} has {address}/{prefix}")
    out = subprocess.run(["/usr/sbin/netstat", "-rn", "-f", "inet"], capture_output=True,
                         text=True, check=False).stdout
    for line in out.splitlines():
        words = line.split()
        if len(words) < 4 or words[3].startswith(("bridge", "lo")):
            continue
        route = _route_network(words[0])
        if route is not None and route.prefixlen > 0 and route.overlaps(net):
            entry = f"{words[3]} routes {route}"
            if entry not in found and not any(e.startswith(f"{words[3]} has") for e in found):
                found.append(entry)
    return found


def installed_subnet(path: Path | None = None) -> str | None:
    """The agent subnet the installed rules (this environment's, or `path`)
    block, or None when there are none."""
    try:
        rules = (path or RULES_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in rules.splitlines():
        words = line.split()
        # block drop in quick inet from <subnet> to ! <subnet>
        if words[:6] == ["block", "drop", "in", "quick", "inet", "from"] and "!" in words:
            return words[6]
    return None


def installed_subnet_v6(path: Path | None = None) -> str | None:
    """The agent IPv6 /64 the installed rules (this environment's, or `path`)
    block, or None. The rules are where setup records it."""
    try:
        rules = (path or RULES_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in rules.splitlines():
        words = line.split()
        # block drop in quick inet6 from <subnet> to ! <subnet>
        if words[:6] == ["block", "drop", "in", "quick", "inet6", "from"] and "!" in words:
            return words[6]
    return None


def agent_subnet_v6() -> str:
    """The agent network's IPv6 /64: the installed firewall's, else this
    environment's first candidate (live's fixed one)."""
    return installed_subnet_v6() or macos_agent_subnet_v6()


def agent_subnet() -> str:
    """The agent network's IPv4 subnet: the installed firewall's, else the
    first candidate."""
    return installed_subnet() or CANDIDATE_SUBNETS[0]


def _other_rules(rules_dir: Path | None = None) -> list[Path]:
    directory = rules_dir or _RULES_DIR
    try:
        return [p for p in directory.iterdir()
                if (p.name == LIVE_REVERSE_DNS or p.name.startswith(f"{LIVE_REVERSE_DNS}."))
                and p.name != RULES_PATH.name]
    except OSError:
        return []


def other_environments_subnets(rules_dir: Path | None = None) -> set[str]:
    """Agent subnets other environments' installed anchors claim, so two
    environments never share one (their networks may not be up to show a
    conflict)."""
    return {s for s in (installed_subnet(p) for p in _other_rules(rules_dir)) if s}


def other_environments_subnets_v6(rules_dir: Path | None = None) -> set[str]:
    """Agent IPv6 /64s other environments' installed anchors claim."""
    return {s for s in (installed_subnet_v6(p) for p in _other_rules(rules_dir)) if s}


def choose_subnet_v6(rules_dir: Path | None = None) -> str:
    """The IPv6 /64 setup should install: the installed one, else this
    environment's first candidate no other environment's anchor claims.
    Installing it records it."""
    current = installed_subnet_v6()
    if current:
        return current
    others = other_environments_subnets_v6(rules_dir)
    for candidate in macos_agent_subnet_v6_candidates():
        if candidate not in others:
            return candidate
    raise RuntimeError("every candidate agent IPv6 /64 is taken by another environment")


def choose_subnet() -> tuple[str, list[str]]:
    """The subnet setup should install, and what (if anything) ruled out the
    one installed now. Keeps the installed subnet while nothing else uses it."""
    current = installed_subnet()
    rejected = conflicts(current) if current else []
    if current and not rejected:
        return current, []
    others = other_environments_subnets()
    for candidate in CANDIDATE_SUBNETS:
        if candidate not in others and not conflicts(candidate):
            return candidate, rejected
    raise RuntimeError("every candidate agent subnet is in use on this Mac: "
                       + ", ".join(CANDIDATE_SUBNETS))


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
    "CANDIDATE_SUBNETS", "agent_bridge", "agent_subnet", "choose_subnet", "conflicts",
    "agent_subnet_v6", "choose_subnet_v6", "installed_subnet_v6",
    "other_environments_subnets", "other_environments_subnets_v6",
    "host_address", "install", "install_commands", "installed_subnet", "ipv6_off",
    "render_daemon", "render_rules", "state", "wait_ipv6_off",
    "uninstall", "uninstall_commands",
]
