"""Firecracker network pool: constants, IP math, allocation, and the
config renderers (shell command + NixOS module) shown to operators.

The Firecracker backend needs a privileged one-time network setup:
a pool of point-to-point TAP devices (owned by the invoking user, so
`bot-bottle start` never needs root) and a dedicated nftables table that
isolates every VM. The pool parameters live in exactly one place —
`netpool.defaults.env`, a plain KEY=VALUE file next to this module —
and every consumer reads *that*: this module (below), the shell script
(`scripts/firecracker-netpool.sh`), and the NixOS module
(`nix/firecracker-netpool.nix`). The `InfraSettings.fc_*` fields (the
CLI fills them from the `BOT_BOTTLE_FC_*` env vars) override the file,
and the backend's fail-closed preflight derives from `NetPool`, so
nothing can drift.

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
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ...contract.hostctl import InfraSettings
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


def _default(key: str) -> str:
    try:
        return _DEFAULTS[key]
    except KeyError:
        die(f"{key} is missing from {DEFAULTS_FILE.name} (broken install)")


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


@dataclass(frozen=True)
class NetPool:
    """The pool's parameters: `netpool.defaults.env`, overridden by settings.

    Interface names are capped at 15 chars (IFNAMSIZ-1); "bbfc" + a small
    index stays well under that and is distinctive enough to grep for."""

    pool_size: int
    ip_base: str
    iface_prefix: str
    nft_table: str
    # The orchestrator VM's dedicated TAP — outside the bbfc* agent pool and,
    # unlike it, NAT'd to the internet (see `orch_slot`). The orchestrator is
    # trusted infra: it builds agent images in-VM (buildah needs to FROM-pull
    # + apt/npm).
    orch_iface: str
    # The gateway (data-plane) VM's dedicated TAP — the second infra link (see
    # `gw_slot`), separate from the orchestrator's. Like the
    # orchestrator link it is NAT'd to the internet (the gateway forwards agent
    # egress upstream); unlike it, agents DNAT here, never to the orchestrator,
    # so a breached agent has no L3 route to the control plane.
    gw_iface: str

    def slot(self, index: int) -> Slot:
        base = int(ipaddress.IPv4Address(self.ip_base))
        return Slot(
            index=index,
            iface=f"{self.iface_prefix}{index}",
            host_ip=str(ipaddress.IPv4Address(base + 2 * index)),
            guest_ip=str(ipaddress.IPv4Address(base + 2 * index + 1)),
        )

    def all_slots(self) -> list[Slot]:
        return [self.slot(i) for i in range(self.pool_size)]

    def orch_slot(self) -> Slot:
        """The orchestrator VM's dedicated link — its own TAP (`orch_iface`)
        on a /31 at the TOP of the IP_BASE /16 (host x.y.255.0, guest
        x.y.255.1), well clear of the agent pool near the bottom of the
        block. Unlike a pool `Slot`, this link is NAT'd out to the internet
        by the setup (the orchestrator is trusted infra), so it is
        deliberately *not* one of the isolated `bbfc*` slots.

        `index` is -1 (sentinel: not a pool index)."""
        base16 = int(ipaddress.IPv4Address(self.ip_base)) & 0xFFFF0000
        host = base16 + 0xFF00
        return Slot(
            index=-1,
            iface=self.orch_iface,
            host_ip=str(ipaddress.IPv4Address(host)),
            guest_ip=str(ipaddress.IPv4Address(host + 1)),
        )

    def gw_slot(self) -> Slot:
        """The gateway (data-plane) VM's dedicated link — its own TAP
        (`gw_iface`) on the /31 immediately above the orchestrator link (host
        x.y.255.2, guest x.y.255.3), still clear of the agent pool at the
        bottom of the block. Mirrors `orch_slot`: NAT'd to the internet (the
        gateway forwards agent egress upstream), not one of the isolated
        `bbfc*` slots. Agents DNAT their gateway-port traffic to this guest.

        `index` is -2 (sentinel: not a pool index)."""
        base16 = int(ipaddress.IPv4Address(self.ip_base)) & 0xFFFF0000
        host = base16 + 0xFF02
        return Slot(
            index=-2,
            iface=self.gw_iface,
            host_ip=str(ipaddress.IPv4Address(host)),
            guest_ip=str(ipaddress.IPv4Address(host + 1)),
        )

    def nft_table_present(self) -> bool:
        """True iff the isolation table exists in the active nftables
        backend. The backend's preflight treats absence as fatal — the VM
        must not boot without its egress boundary in place.

        Listing may require root; when it can't be confirmed here the
        launch path falls back to an empirical post-boot isolation probe.
        Returns False (fail-closed) if `nft` is unavailable."""
        return _run_ok(["nft", "list", "table", "inet", self.nft_table])

    def missing_taps(self) -> list[str]:
        """Pool TAPs that the one-time setup has not created yet."""
        return [s.iface for s in self.all_slots() if not tap_present(s.iface)]

    def pool_span(self) -> tuple[int, int]:
        """Inclusive [first, last] integer bounds of every address the pool
        occupies: base .. base + 2*pool_size - 1."""
        base = int(ipaddress.IPv4Address(self.ip_base))
        return base, base + 2 * self.pool_size - 1

    def overlapping_routes(self) -> list["RouteConflict"]:
        """Existing routes whose destination overlaps the pool's address
        range, excluding our own `bbfc*` TAP routes and the default route.

        A non-empty result means the IP base collides with something already
        configured on this host, so the pool would shadow (or be shadowed by)
        it. Empty on success — or when `ip` is missing or unparseable, since
        this is an advisory guard, not the fail-closed isolation check (that's
        the nft table + post-boot probe)."""
        return _overlapping_routes(self.pool_span(), self.iface_prefix)

    def allocate(self, slug: str) -> tuple[Slot, IO[str]]:
        """Claim a free pool slot for one bottle. Returns the slot and the
        held lock file — the caller keeps it open for the VM's lifetime and
        closes it on teardown; the flock auto-releases if the launcher
        crashes, so a slot is never leaked. Dies when the pool is
        exhausted.

        A per-slot `flock` (rather than inspecting running processes) makes
        allocation race-free across concurrent launches without a central
        registry: the first launcher to grab the lock owns the slot."""
        del slug  # logged by the caller; allocation is purely lock-driven
        for s in self.all_slots():
            lock_path = _lock_dir() / f"{s.iface}.lock"
            handle = open(lock_path, "w", encoding="utf-8")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                continue
            return s, handle
        die(f"Firecracker TAP pool exhausted ({self.pool_size} slots, all in "
            f"use). Stop a running bottle or raise BOT_BOTTLE_FC_POOL_SIZE "
            f"and re-run `bot-bottle backend setup --backend=firecracker`.")
        raise AssertionError("unreachable")

    def nondefault_env(self) -> str:
        """The pool env overrides that differ from the shared defaults, so the
        printed shell command reproduces the operator's current settings."""
        pairs = []
        for key, value in (("BOT_BOTTLE_FC_POOL_SIZE", str(self.pool_size)),
                           ("BOT_BOTTLE_FC_IP_BASE", self.ip_base),
                           ("BOT_BOTTLE_FC_IFACE_PREFIX", self.iface_prefix)):
            if value != _default(key):
                pairs.append(f"{key}={value}")
        return " ".join(pairs)


def from_settings(infra: InfraSettings) -> NetPool:
    """The pool `infra` describes; unset fields take `netpool.defaults.env`."""
    return NetPool(
        pool_size=infra.fc_pool_size or int(_default("BOT_BOTTLE_FC_POOL_SIZE")),
        ip_base=infra.fc_ip_base or _default("BOT_BOTTLE_FC_IP_BASE"),
        iface_prefix=infra.fc_iface_prefix or _default("BOT_BOTTLE_FC_IFACE_PREFIX"),
        nft_table=infra.fc_nft_table or _default("BOT_BOTTLE_FC_NFT_TABLE"),
        orch_iface=infra.fc_orch_iface or _default("BOT_BOTTLE_FC_ORCH_IFACE"),
        gw_iface=infra.fc_gw_iface or _default("BOT_BOTTLE_FC_GW_IFACE"),
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


def tap_present(iface: str) -> bool:
    # `ip link show` is unprivileged, so the TAP-pool check is reliable
    # for the non-root launcher.
    return _run_ok(["ip", "link", "show", iface])


@dataclass(frozen=True)
class RouteConflict:
    """An existing host route whose destination overlaps the pool range
    but isn't one of our own bbfc TAPs — i.e. the chosen IP base clashes
    with something already on the host (a Tailscale CGNAT peer, a
    docker/libvirt bridge, the LAN…)."""

    dst: str
    dev: str


def _overlapping_routes(span: tuple[int, int], iface_prefix: str) -> list[RouteConflict]:
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

    lo, hi = span
    conflicts: list[RouteConflict] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        dst = r.get("dst")
        dev = r.get("dev", "")
        if not isinstance(dst, str) or dst in ("", "default"):
            continue
        if isinstance(dev, str) and dev.startswith(iface_prefix):
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


# --- config renderers (shown by `bot-bottle backend setup`) -----------

# The persistent unit is the portable install: the same systemd oneshot
# on every systemd distro (Debian/Ubuntu/Fedora/RHEL/Arch/…).
SYSTEMD_UNIT = "bot-bottle-firecracker-netpool.service"


def render_shell_setup(pool: NetPool) -> str:
    """The imperative one-shot command — non-persistent fallback for
    hosts without systemd (OpenRC/runit/manual)."""
    env = pool.nondefault_env()
    prefix = f"{env} " if env else ""
    return f"sudo {prefix}./scripts/firecracker-netpool.sh up"


def render_systemd_unit(pool: NetPool, owner: str, script_path: str) -> str:
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
Environment=BOT_BOTTLE_FC_POOL_SIZE={pool.pool_size}
Environment=BOT_BOTTLE_FC_IP_BASE={pool.ip_base}
Environment=BOT_BOTTLE_FC_IFACE_PREFIX={pool.iface_prefix}
Environment=BOT_BOTTLE_FC_OWNER={owner}
ExecStart={script_path} up
ExecStop={script_path} down

[Install]
WantedBy=multi-user.target
"""


# The NixOS setup is a real, importable module (nix/firecracker-netpool.nix,
# exposed as the flake output nixosModules.firecracker-netpool) rather than a
# generated paste — see `backend setup` output.
