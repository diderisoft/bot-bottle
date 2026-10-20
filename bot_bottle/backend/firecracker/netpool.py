"""Firecracker network pool: constants, IP math, allocation, and the
config renderers (shell command + NixOS module) shown to operators.

The Firecracker backend needs a privileged one-time network setup:
a pool of point-to-point TAP devices (owned by the invoking user, so
`bot-bottle start` never needs root) and a dedicated nftables table that
isolates every VM. The pool parameters live in exactly one place —
`netpool.defaults.env`, a plain KEY=VALUE file next to this module —
and every consumer reads *that*: this module (below), the shell script
(`scripts/firecracker-netpool.sh`), and the NixOS module
(`nix/firecracker-netpool.nix`). A `BOT_BOTTLE_FC_*` env var of the
same name always overrides the file, and the backend's fail-closed
preflight derives from these accessors, so nothing can drift.

Topology (per slot i):
  * TAP  ``bbfc{i}``  — no shared bridge, so no docker0 / virbr0 / cni0
    / br-* collisions.
  * a /31 host<->guest link: host = base + 2i (the gateway the VM
    routes through), guest = base + 2i + 1 (the VM's address).
  * isolation via ``table inet bot_bottle_fc``: a VM reaches only its
    own gateway (DNAT'd from the host TAP IP) and nothing else.

The default IP block is ``10.243.0.0/16`` — an intentionally obscure
corner of RFC-1918 private space. RFC-1918 is the range *designated*
for private links; we pick a high, unusual /16 to steer clear of the
common occupants (Docker's 172.17-31, libvirt's 192.168.122, k8s
10.42/10.244, and typical home LANs on 192.168.0/1.x or 10.0.0.x).
We deliberately avoid ``100.64.0.0/10`` (RFC-6598 CGNAT) because
Tailscale hands out node addresses from exactly that range. No default
is collision-proof, so `overlapping_routes()` is the real guard —
`backend setup`/`status` and the launch preflight call it to catch
a base that clashes with something already on the host.
"""

from __future__ import annotations

import fcntl
import ipaddress
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ...lib.log import die


# The pool defaults live in one shared file (see module docstring); the
# shell script and NixOS module read the same file, so the values can't
# drift. This is a packaged data file — a missing/broken install is a
# hard error, surfaced here rather than as confusing empty defaults.
DEFAULTS_FILE = Path(__file__).with_name("netpool.defaults.env")


def _load_defaults() -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in DEFAULTS_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


_DEFAULTS = _load_defaults()


def _cfg(key: str) -> str:
    """A `BOT_BOTTLE_FC_*` env var overrides the shared-file default."""
    try:
        return os.environ.get(key) or _DEFAULTS[key]
    except KeyError:
        die(f"{key} is missing from {DEFAULTS_FILE.name} (broken install)")


# Interface names are capped at 15 chars (IFNAMSIZ-1); "bbfc" + a small
# index stays well under that and is distinctive enough to grep for.
IFACE_PREFIX = _cfg("BOT_BOTTLE_FC_IFACE_PREFIX")
NFT_TABLE = _cfg("BOT_BOTTLE_FC_NFT_TABLE")

# The orchestrator VM's dedicated TAP — outside the bbfc* agent pool
# and, unlike it, NAT'd to the internet (see `orch_slot`). The
# orchestrator is trusted infra: it builds agent images in-VM (buildah
# needs to FROM-pull + apt/npm).
ORCH_IFACE = _cfg("BOT_BOTTLE_FC_ORCH_IFACE")

# The gateway (data-plane) VM's dedicated TAP — the second infra link
# (see `gw_slot`), split from the orchestrator per PRD 0070. Like the
# orchestrator link it is NAT'd to the internet (the gateway forwards
# agent egress upstream); unlike it, agents DNAT here, never to the
# orchestrator, so a breached agent has no L3 route to the control plane.
GW_IFACE = _cfg("BOT_BOTTLE_FC_GW_IFACE")


def pool_size() -> int:
    return int(_cfg("BOT_BOTTLE_FC_POOL_SIZE"))


def ip_base() -> str:
    return _cfg("BOT_BOTTLE_FC_IP_BASE")


# Gateway ports the VM reaches at its host-side TAP IP. Kept in sync
# with the backend constants (egress 9099, supervise 9100, git-http
# 9420); rendered into the setup output for operator visibility.
GATEWAY_PORTS = (9099, 9100, 9420)


@dataclass(frozen=True)
class Slot:
    """One pool slot: a TAP device and its host/guest /31 addresses."""

    index: int
    iface: str
    host_ip: str
    guest_ip: str

    @property
    def guest_cidr(self) -> str:
        """Guest address with the /31 prefix, for the kernel `ip=` arg."""
        return f"{self.guest_ip}/31"


def slot(index: int) -> Slot:
    base = int(ipaddress.IPv4Address(ip_base()))
    return Slot(
        index=index,
        iface=f"{IFACE_PREFIX}{index}",
        host_ip=str(ipaddress.IPv4Address(base + 2 * index)),
        guest_ip=str(ipaddress.IPv4Address(base + 2 * index + 1)),
    )


def all_slots() -> list[Slot]:
    return [slot(i) for i in range(pool_size())]


def orch_slot() -> Slot:
    """The orchestrator VM's dedicated link — its own TAP (`ORCH_IFACE`)
    on a /31 at the TOP of the IP_BASE /16 (host x.y.255.0, guest
    x.y.255.1), well clear of the agent pool near the bottom of the
    block. Unlike a pool `Slot`, this link is NAT'd out to the internet
    by the setup (the orchestrator is trusted infra), so it is
    deliberately *not* one of the isolated `bbfc*` slots.

    `index` is -1 (sentinel: not a pool index)."""
    base16 = int(ipaddress.IPv4Address(ip_base())) & 0xFFFF0000
    host = base16 + 0xFF00
    return Slot(
        index=-1,
        iface=ORCH_IFACE,
        host_ip=str(ipaddress.IPv4Address(host)),
        guest_ip=str(ipaddress.IPv4Address(host + 1)),
    )


def gw_slot() -> Slot:
    """The gateway (data-plane) VM's dedicated link — its own TAP
    (`GW_IFACE`) on the /31 immediately above the orchestrator link (host
    x.y.255.2, guest x.y.255.3), still clear of the agent pool at the
    bottom of the block. Mirrors `orch_slot`: NAT'd to the internet (the
    gateway forwards agent egress upstream), not one of the isolated
    `bbfc*` slots. Agents DNAT their gateway-port traffic to this guest.

    `index` is -2 (sentinel: not a pool index)."""
    base16 = int(ipaddress.IPv4Address(ip_base())) & 0xFFFF0000
    host = base16 + 0xFF02
    return Slot(
        index=-2,
        iface=GW_IFACE,
        host_ip=str(ipaddress.IPv4Address(host)),
        guest_ip=str(ipaddress.IPv4Address(host + 1)),
    )


# --- fail-closed verification ---------------------------------------

def _run_ok(argv: list[str]) -> bool:
    """Run a probe command, treating an unavailable binary as failure
    (rather than crashing) so callers can stay fail-closed."""
    try:
        return subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        # Not only "missing". A name on PATH that isn't executable by this
        # user raises PermissionError, and CPython reports that EACCES in
        # preference to the ENOENT from the other PATH entries — which is
        # how `doctor` came to die with a traceback on a fresh macOS
        # account. Any OSError means the probe couldn't run, which for a
        # fail-closed check is indistinguishable from "not present".
        return False


def nft_table_present() -> bool:
    """True iff the isolation table exists in the active nftables
    backend. The backend's preflight treats absence as fatal — the VM
    must not boot without its egress boundary in place.

    Listing may require root; when it can't be confirmed here the
    launch path falls back to an empirical post-boot isolation probe.
    Returns False (fail-closed) if `nft` is unavailable."""
    return _run_ok(["nft", "list", "table", "inet", NFT_TABLE])


def tap_present(iface: str) -> bool:
    # `ip link show` is unprivileged, so the TAP-pool check is reliable
    # for the non-root launcher.
    return _run_ok(["ip", "link", "show", iface])


def missing_taps() -> list[str]:
    """Pool TAPs that the one-time setup has not created yet."""
    return [s.iface for s in all_slots() if not tap_present(s.iface)]


@dataclass(frozen=True)
class RouteConflict:
    """An existing host route whose destination overlaps the pool range
    but isn't one of our own bbfc TAPs — i.e. the chosen IP base clashes
    with something already on the host (a Tailscale CGNAT peer, a
    docker/libvirt bridge, the LAN…)."""

    dst: str
    dev: str


def _pool_span() -> tuple[int, int]:
    """Inclusive [first, last] integer bounds of every address the pool
    occupies: base .. base + 2*pool_size - 1."""
    base = int(ipaddress.IPv4Address(ip_base()))
    return base, base + 2 * pool_size() - 1


def overlapping_routes() -> list[RouteConflict]:
    """Existing routes whose destination overlaps the pool's address
    range, excluding our own `bbfc*` TAP routes and the default route.

    A non-empty result means `BOT_BOTTLE_FC_IP_BASE` collides with
    something already configured on this host, so the pool would shadow
    (or be shadowed by) it. Empty on success — or when `ip` is missing
    or unparseable, since this is an advisory guard, not the fail-closed
    isolation check (that's the nft table + post-boot probe)."""
    import json

    try:
        proc = subprocess.run(
            ["ip", "-json", "route", "show", "table", "all"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        # Missing, or present-but-not-executable for this user; either way
        # there are no routes we can enumerate. See _run_ok.
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        routes = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    lo, hi = _pool_span()
    conflicts: list[RouteConflict] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        dst = r.get("dst")
        dev = r.get("dev", "")
        if not isinstance(dst, str) or dst in ("", "default"):
            continue
        if isinstance(dev, str) and dev.startswith(IFACE_PREFIX):
            continue  # our own pool link
        try:
            net = ipaddress.ip_network(dst, strict=False)
        except ValueError:
            continue
        if net.version != 4:
            continue
        r_lo = int(net.network_address)
        r_hi = int(net.broadcast_address)
        if r_lo <= hi and lo <= r_hi:  # ranges intersect
            conflicts.append(RouteConflict(dst=dst, dev=str(dev)))
    return conflicts


# --- allocation ------------------------------------------------------

def _lock_dir() -> Path:
    d = Path.home() / ".cache" / "bot-bottle" / "firecracker" / "pool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def allocate(slug: str) -> tuple[Slot, IO[str]]:
    """Claim a free pool slot for one bottle. Returns the slot and the
    held lock file — the caller keeps it open for the VM's lifetime and
    closes it on teardown; the flock auto-releases if the launcher
    crashes, so a slot is never leaked. Dies when the pool is
    exhausted.

    A per-slot `flock` (rather than inspecting running processes) makes
    allocation race-free across concurrent launches without a central
    registry: the first launcher to grab the lock owns the slot."""
    del slug  # logged by the caller; allocation is purely lock-driven
    for s in all_slots():
        lock_path = _lock_dir() / f"{s.iface}.lock"
        handle = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            continue
        return s, handle
    die(f"Firecracker TAP pool exhausted ({pool_size()} slots, all in "
        f"use). Stop a running bottle or raise BOT_BOTTLE_FC_POOL_SIZE "
        f"and re-run `bot-bottle backend setup --backend=firecracker`.")
    raise AssertionError("unreachable")


# --- config renderers (shown by `bot-bottle backend setup`) -----------

# The persistent unit is the portable install: the same systemd oneshot
# on every systemd distro (Debian/Ubuntu/Fedora/RHEL/Arch/…).
SYSTEMD_UNIT = "bot-bottle-firecracker-netpool.service"


def render_shell_setup() -> str:
    """The imperative one-shot command — non-persistent fallback for
    hosts without systemd (OpenRC/runit/manual)."""
    env = _nondefault_env()
    prefix = f"{env} " if env else ""
    return f"sudo {prefix}./scripts/firecracker-netpool.sh up"


def render_systemd_unit(owner: str, script_path: str) -> str:
    """A portable systemd oneshot unit for the pool — identical across
    every systemd distro. ExecStart/ExecStop delegate to the bundled
    shell script (the single source of bring-up logic); pool params are
    pinned via Environment= so the unit matches the CLI's current
    settings and doesn't depend on $SUDO_USER at boot (systemd runs it
    as root with no SUDO_USER, which would otherwise own the TAPs as
    root and break the rootless launch)."""
    return f"""[Unit]
Description=bot-bottle Firecracker TAP pool + nft isolation table
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=BOT_BOTTLE_FC_POOL_SIZE={pool_size()}
Environment=BOT_BOTTLE_FC_IP_BASE={ip_base()}
Environment=BOT_BOTTLE_FC_IFACE_PREFIX={IFACE_PREFIX}
Environment=BOT_BOTTLE_FC_OWNER={owner}
ExecStart={script_path} up
ExecStop={script_path} down

[Install]
WantedBy=multi-user.target
"""


# The NixOS setup is a real, importable module (nix/firecracker-netpool.nix,
# exposed as the flake output nixosModules.firecracker-netpool) rather than a
# generated paste — see `backend setup` output.


def _nondefault_env() -> str:
    """Render any non-default pool env overrides so the printed shell
    command reproduces the operator's current settings."""
    pairs = []
    if os.environ.get("BOT_BOTTLE_FC_POOL_SIZE"):
        pairs.append(f"BOT_BOTTLE_FC_POOL_SIZE={pool_size()}")
    if os.environ.get("BOT_BOTTLE_FC_IP_BASE"):
        pairs.append(f"BOT_BOTTLE_FC_IP_BASE={ip_base()}")
    if os.environ.get("BOT_BOTTLE_FC_IFACE_PREFIX"):
        pairs.append(f"BOT_BOTTLE_FC_IFACE_PREFIX={IFACE_PREFIX}")
    return " ".join(pairs)
