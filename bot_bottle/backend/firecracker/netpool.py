"""Firecracker network pool: constants, IP math, slot allocation, and the setup
renderers (shell, systemd, NixOS).

The one-time privileged setup creates user-owned TAPs and the `inet
bot_bottle_fc` nft table. Pool parameters live only in `netpool.defaults.env`,
read by this module, `scripts/firecracker-netpool.sh` and
`nix/firecracker-netpool.nix`; `InfraSettings.fc_*` (from `BOT_BOTTLE_FC_*`)
override it.

Per slot i: TAP `bbfc{i}` with no shared bridge, a /31 with host base + 2i and
guest base + 2i + 1. A VM reaches only its own gateway, DNAT'd from the host
TAP IP.

The default block `10.243.0.0/16` avoids Docker, libvirt, k8s, common LANs and
Tailscale's 100.64.0.0/10. `overlapping_routes()` catches collisions.
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


# A packaged data file; a missing one is a hard error.
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




@dataclass(frozen=True)
class Slot:
    """One pool slot: a TAP and its /31 addresses."""

    index: int
    iface: str
    host_ip: str
    guest_ip: str

@dataclass(frozen=True)
class NetPool:
    """Pool parameters: `netpool.defaults.env` overridden by settings.
    Interface names are capped at 15 chars."""

    pool_size: int
    ip_base: str
    iface_prefix: str
    nft_table: str
    # The orchestrator VM's TAP, outside the agent pool and NAT'd out (it pulls
    # bases and packages for builds).
    orch_iface: str
    # The gateway VM's TAP, also NAT'd out. Agents DNAT here, never to the
    # orchestrator.
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
        """The orchestrator VM's link: its own TAP on the /31 at the top of the
        block (host x.y.255.0, guest x.y.255.1), NAT'd out. `index` is -1."""
        base16 = int(ipaddress.IPv4Address(self.ip_base)) & 0xFFFF0000
        host = base16 + 0xFF00
        return Slot(
            index=-1,
            iface=self.orch_iface,
            host_ip=str(ipaddress.IPv4Address(host)),
            guest_ip=str(ipaddress.IPv4Address(host + 1)),
        )

    def gw_slot(self) -> Slot:
        """The gateway VM's link: the /31 above the orchestrator's (host
        x.y.255.2, guest x.y.255.3), NAT'd out. `index` is -2."""
        base16 = int(ipaddress.IPv4Address(self.ip_base)) & 0xFFFF0000
        host = base16 + 0xFF02
        return Slot(
            index=-2,
            iface=self.gw_iface,
            host_ip=str(ipaddress.IPv4Address(host)),
            guest_ip=str(ipaddress.IPv4Address(host + 1)),
        )

    def nft_table_present(self) -> bool:
        """True iff the isolation table exists. Listing may need root; when it
        can't be confirmed, launch relies on the post-boot probe. False if
        `nft` is unavailable."""
        return _run_ok(["nft", "list", "table", "inet", self.nft_table])

    def missing_taps(self) -> list[str]:
        """Pool TAPs the setup hasn't created."""
        return [s.iface for s in self.all_slots() if not tap_present(s.iface)]

    def pool_span(self) -> tuple[int, int]:
        """Inclusive integer bounds of the pool's addresses."""
        base = int(ipaddress.IPv4Address(self.ip_base))
        return base, base + 2 * self.pool_size - 1

    def overlapping_routes(self) -> list["RouteConflict"]:
        """Existing routes overlapping the pool, excluding our TAPs and the
        default route. Advisory: empty when `ip` is missing or unparseable."""
        return _overlapping_routes(self.pool_span(), self.iface_prefix)

    def allocate(self, slug: str) -> tuple[Slot, IO[str]]:
        """Claim a free slot with a per-slot flock. Returns the slot and the
        held lock file, which the caller closes on teardown (or the OS on a
        crash). Dies when the pool is exhausted."""
        del slug  # allocation is lock-driven
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

    def reclaim(self, iface: str) -> IO[str] | None:
        """Re-take the lock on `iface`'s slot for a bottle that already runs
        on it, so a new launch can't claim it. None if it's held elsewhere."""
        handle = open(_lock_dir() / f"{iface}.lock", "w", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def nondefault_env(self) -> str:
        """Pool env overrides that differ from the defaults, for the printed
        command."""
        pairs = []
        for key, value in (("BOT_BOTTLE_FC_POOL_SIZE", str(self.pool_size)),
                           ("BOT_BOTTLE_FC_IP_BASE", self.ip_base),
                           ("BOT_BOTTLE_FC_IFACE_PREFIX", self.iface_prefix)):
            if value != _default(key):
                pairs.append(f"{key}={value}")
        return " ".join(pairs)


def from_settings(infra: InfraSettings) -> NetPool:
    """The pool `infra` describes; unset fields take the defaults."""
    return NetPool(
        pool_size=infra.fc_pool_size or int(_default("BOT_BOTTLE_FC_POOL_SIZE")),
        ip_base=infra.fc_ip_base or _default("BOT_BOTTLE_FC_IP_BASE"),
        iface_prefix=infra.fc_iface_prefix or _default("BOT_BOTTLE_FC_IFACE_PREFIX"),
        nft_table=infra.fc_nft_table or _default("BOT_BOTTLE_FC_NFT_TABLE"),
        orch_iface=infra.fc_orch_iface or _default("BOT_BOTTLE_FC_ORCH_IFACE"),
        gw_iface=infra.fc_gw_iface or _default("BOT_BOTTLE_FC_GW_IFACE"),
    )


# --- fail-closed verification ---

def _run_ok(argv: list[str]) -> bool:
    """Run a probe command; an unavailable binary counts as failure."""
    try:
        return subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        # Any OSError (including a non-executable name on PATH) means the probe
        # couldn't run.
        return False


def tap_present(iface: str) -> bool:
    # `ip link show` is unprivileged.
    return _run_ok(["ip", "link", "show", iface])


@dataclass(frozen=True)
class RouteConflict:
    """An existing route overlapping the pool that isn't one of our TAPs."""

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
        # Missing or not executable: no routes to enumerate.
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


# --- allocation ---

def _lock_dir() -> Path:
    d = Path.home() / ".cache" / "bot-bottle" / "firecracker" / "pool"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- config renderers (shown by `bot-bottle backend setup`) ---

# The same systemd oneshot on every systemd distro.
SYSTEMD_UNIT = "bot-bottle-firecracker-netpool.service"


def render_shell_setup(pool: NetPool) -> str:
    """One-shot setup command for hosts without systemd (not persistent)."""
    env = pool.nondefault_env()
    prefix = f"{env} " if env else ""
    return f"sudo {prefix}./scripts/firecracker-netpool.sh up"


def render_systemd_unit(pool: NetPool, owner: str, script_path: str) -> str:
    """A systemd oneshot that runs the bundled script, with pool params pinned
    via `Environment=` (systemd has no SUDO_USER, which would otherwise make
    the TAPs root-owned)."""
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


# NixOS uses the real module (`nix/firecracker-netpool.nix`, flake output
# `nixosModules.firecracker-netpool`).
