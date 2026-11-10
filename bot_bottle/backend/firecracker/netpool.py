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

Environment slices (PRD 0116): the setup reserves the live pool plus
`BOT_BOTTLE_FC_ENV_SLOTS` (default 4) more, one per named environment, so a
named environment never needs root. Slice k (live is 0) is:

| | live (k = 0) | slice k (1..N, N <= 63) |
|---|---|---|
| agent /31s | from `10.243.0.0` | from `10.243.k.0` (base + 256k) |
| agent TAPs | `bbfc0..` | `bb{k}fc0..` |
| nft tables | `bot_bottle_fc{,_nat,_gw}` | `bot_bottle_fc{k}{,_nat,_gw}` |
| orchestrator link | `bborch0`, `10.243.255.0/31` | `bborch{k}`, `10.243.255.4k/31` |
| gateway link | `bbgw0`, `10.243.255.2/31` | `bbgw{k}`, `10.243.255.(4k+2)/31` |

Every slice has the live pool's size, so the reservation costs N * (size + 2)
TAPs. `bb{k}fc*` never matches another slice's `bbfc*` or `bb{j}fc*`
wildcard. A named environment claims the lowest free slice on first use, in a
host-wide directory the setup creates (`slice_claims`, so two users' environments
never share one); `bot-bottle envs rm` releases it.

The default block `10.243.0.0/16` avoids Docker, libvirt, k8s, common LANs and
Tailscale's 100.64.0.0/10. `overlapping_routes()` catches collisions.
"""

from __future__ import annotations

import fcntl
import ipaddress
import os
import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ...contract.hostctl import InfraSettings
from ...environment import LIVE, active_name, env_dir
from ...lib.log import die
from . import slice_claims


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
    # The environment slice (0 for live); moves the infra links' /31s up.
    env_slot: int = 0
    # How many named-environment slices the host setup reserves after live.
    env_slot_count: int = 4

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
        block (host x.y.255.0, guest x.y.255.1; 4k higher for slice k),
        NAT'd out. `index` is -1."""
        base16 = int(ipaddress.IPv4Address(self.ip_base)) & 0xFFFF0000
        host = base16 + 0xFF00 + 4 * self.env_slot
        return Slot(
            index=-1,
            iface=self.orch_iface,
            host_ip=str(ipaddress.IPv4Address(host)),
            guest_ip=str(ipaddress.IPv4Address(host + 1)),
        )

    def gw_slot(self) -> Slot:
        """The gateway VM's link: the /31 above the orchestrator's (host
        x.y.255.2, guest x.y.255.3; 4k higher for slice k), NAT'd out.
        `index` is -2."""
        base16 = int(ipaddress.IPv4Address(self.ip_base)) & 0xFFFF0000
        host = base16 + 0xFF02 + 4 * self.env_slot
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
            f"and re-run `bot-bottle install --backend=firecracker`.")
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
                           ("BOT_BOTTLE_FC_IFACE_PREFIX", self.iface_prefix),
                           ("BOT_BOTTLE_FC_ENV_SLOTS", str(self.env_slot_count))):
            if value != _default(key):
                pairs.append(f"{key}={value}")
        return " ".join(pairs)


# Slices' infra links fit the top /24's 64 four-address blocks (live is 0).
MAX_ENV_SLOTS = 63


@dataclass(frozen=True)
class EnvSlice:
    """Slice `slot`'s default pool base and names (0 is live)."""

    slot: int
    ip_base: str
    iface_prefix: str
    nft_table: str
    orch_iface: str
    gw_iface: str


def env_slots(infra: InfraSettings) -> int:
    """How many named-environment slices the host reserves."""
    return infra.fc_env_slots or int(_default("BOT_BOTTLE_FC_ENV_SLOTS"))


def env_slice(slot: int, pool_size: int = 0) -> EnvSlice:
    """Slice `slot`'s defaults (see the module doc). Raises ValueError for a
    slot outside 0..MAX_ENV_SLOTS, or a pool too big for a slice's /24."""
    if not 0 <= slot <= MAX_ENV_SLOTS:
        raise ValueError(f"environment slice {slot} is outside 0..{MAX_ENV_SLOTS}")
    size = pool_size or int(_default("BOT_BOTTLE_FC_POOL_SIZE"))
    if slot and 2 * size > 256:
        raise ValueError(f"a pool of {size} slots doesn't fit a slice's /24 (at most 128)")
    if slot == 0:
        return EnvSlice(
            slot=0,
            ip_base=_default("BOT_BOTTLE_FC_IP_BASE"),
            iface_prefix=_default("BOT_BOTTLE_FC_IFACE_PREFIX"),
            nft_table=_default("BOT_BOTTLE_FC_NFT_TABLE"),
            orch_iface=_default("BOT_BOTTLE_FC_ORCH_IFACE"),
            gw_iface=_default("BOT_BOTTLE_FC_GW_IFACE"),
        )
    base = int(ipaddress.IPv4Address(_default("BOT_BOTTLE_FC_IP_BASE")))
    return EnvSlice(
        slot=slot,
        ip_base=str(ipaddress.IPv4Address(base + 256 * slot)),
        iface_prefix=f"bb{slot}fc",
        nft_table=f"{_default('BOT_BOTTLE_FC_NFT_TABLE')}{slot}",
        orch_iface=f"bborch{slot}",
        gw_iface=f"bbgw{slot}",
    )


def from_settings(infra: InfraSettings, env: str | None = None) -> NetPool:
    """The pool `infra` describes for environment `env` (default: the active
    one); unset fields take its slice's defaults. A named environment claims
    a slice on first use, and dies saying how to reserve more when every
    reserved one is held."""
    name = active_name() if env is None else env
    count = min(env_slots(infra), MAX_ENV_SLOTS)
    slot = 0
    if name != LIVE:
        try:
            slot = claim_slice(infra, name, count)
        except slice_claims.SliceClaimError as e:
            die(str(e))
    try:
        sl = env_slice(slot, infra.fc_pool_size)
    except ValueError as e:
        die(f"firecracker network pool: {e}")
    # A pool configured explicitly (the CI runner's) is its own single slice:
    # its infra TAPs are named outright, so its links stay at the pool's base
    # whatever host-wide slice this environment claimed.
    explicit = bool(infra.fc_ip_base or infra.fc_orch_iface or infra.fc_gw_iface)
    return NetPool(
        pool_size=infra.fc_pool_size or int(_default("BOT_BOTTLE_FC_POOL_SIZE")),
        ip_base=infra.fc_ip_base or sl.ip_base,
        iface_prefix=infra.fc_iface_prefix or sl.iface_prefix,
        nft_table=infra.fc_nft_table or sl.nft_table,
        orch_iface=infra.fc_orch_iface or sl.orch_iface,
        gw_iface=infra.fc_gw_iface or sl.gw_iface,
        env_slot=0 if explicit else slot,
        env_slot_count=count,
    )


def slices_dir(infra: InfraSettings) -> Path:
    """The host-wide slice claims directory the setup creates."""
    return Path(infra.fc_slices_dir or _default("BOT_BOTTLE_FC_SLICES_DIR"))


def _carrier(iface: str) -> bool:
    """Whether a process holds `iface` open: a TAP has carrier only then."""
    try:
        return Path(f"/sys/class/net/{iface}/carrier").read_text(encoding="ascii").strip() == "1"
    except OSError:  # absent, or down
        return False


def slice_in_use(slot: int, pool_size: int = 0) -> bool:
    """Whether any of slice `slot`'s TAPs is attached to a running VM."""
    sl = env_slice(slot, pool_size)
    size = pool_size or int(_default("BOT_BOTTLE_FC_POOL_SIZE"))
    ifaces = [sl.orch_iface, sl.gw_iface, *(f"{sl.iface_prefix}{i}" for i in range(size))]
    return any(_carrier(i) for i in ifaces)


def _user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return ""


def claim_slice(infra: InfraSettings, name: str, count: int) -> int:
    """This user's environment `name`'s slice, claimed host-wide."""
    return slice_claims.claim(
        slices_dir(infra), env=name, count=count, uid=os.getuid(), user=_user(),
        registration=env_dir(name),
        in_use=lambda k: slice_in_use(k, infra.fc_pool_size))


def held_slice(infra: InfraSettings, name: str) -> int | None:
    """The slice this user's environment `name` holds, if any."""
    return slice_claims.find(slices_dir(infra), name, os.getuid())


def release_slice(infra: InfraSettings, name: str) -> bool:
    """Give back this user's environment `name`'s slice."""
    return slice_claims.release(slices_dir(infra), env=name, uid=os.getuid())


def host_pool(infra: InfraSettings) -> NetPool:
    """The live pool, which the host setup provisions with every slice after
    it, whichever environment is active. A named environment's own `fc_*`
    overrides describe its slice, not the host's, so they're dropped."""
    if active_name() == LIVE:
        return from_settings(infra, env=LIVE)
    return from_settings(InfraSettings(fc_pool_size=infra.fc_pool_size,
                                       fc_env_slots=infra.fc_env_slots,
                                       fc_slices_dir=infra.fc_slices_dir), env=LIVE)


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


# --- config renderers (shown by `bot-bottle install`) ---

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
Environment=BOT_BOTTLE_FC_ENV_SLOTS={pool.env_slot_count}
Environment=BOT_BOTTLE_FC_OWNER={owner}
ExecStart={script_path} up
ExecStop={script_path} down

[Install]
WantedBy=multi-user.target
"""


# NixOS uses the real module (`nix/firecracker-netpool.nix`, flake output
# `nixosModules.firecracker-netpool`).
