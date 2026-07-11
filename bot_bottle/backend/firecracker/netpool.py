"""Firecracker network pool: constants, IP math, allocation, and the
config renderers (shell command + NixOS module) shown to operators.

The Firecracker backend needs a privileged one-time network setup:
a pool of point-to-point TAP devices (owned by the invoking user, so
`./cli.py start` never needs root) and a dedicated nftables table that
isolates every VM. This module is the single source of truth for the
pool parameters — the shell script (`scripts/firecracker-netpool.sh`),
the printed NixOS snippet, and the backend's fail-closed preflight all
derive from the constants here so they can't drift.

Topology (per slot i):
  * TAP  ``bbfc{i}``  — no shared bridge, so no docker0 / virbr0 / cni0
    / br-* collisions.
  * a /31 host<->guest link: host = base + 2i (the gateway the VM
    routes through), guest = base + 2i + 1 (the VM's address).
  * isolation via ``table inet bot_bottle_fc``: a VM reaches only its
    own sidecar (DNAT'd from the host TAP IP) and nothing else.

The default IP block is RFC-6598 shared address space (100.64.0.0/10),
which is purpose-built to avoid colliding with LAN/VPN private ranges.
"""

from __future__ import annotations

import fcntl
import ipaddress
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ...log import die


# Interface names are capped at 15 chars (IFNAMSIZ-1); "bbfc" + a small
# index stays well under that and is distinctive enough to grep for.
IFACE_PREFIX = os.environ.get("BOT_BOTTLE_FC_IFACE_PREFIX", "bbfc")
NFT_TABLE = "bot_bottle_fc"


def pool_size() -> int:
    return int(os.environ.get("BOT_BOTTLE_FC_POOL_SIZE", "8"))


def ip_base() -> str:
    return os.environ.get("BOT_BOTTLE_FC_IP_BASE", "100.64.0.0")


# Sidecar ports the VM reaches at its host-side TAP IP. Kept in sync
# with the backend constants (egress 9099, supervise 9100, git-http
# 9420); rendered into the setup output for operator visibility.
SIDECAR_PORTS = (9099, 9100, 9420)


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


# --- fail-closed verification ---------------------------------------

def _run_ok(argv: list[str]) -> bool:
    """Run a probe command, treating a missing binary as failure
    (rather than crashing) so callers can stay fail-closed."""
    try:
        return subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except FileNotFoundError:
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
        f"and re-run `./cli.py firecracker setup`.")
    raise AssertionError("unreachable")


# --- config renderers (shown by `./cli.py firecracker setup`) --------

def render_shell_setup() -> str:
    """The imperative command for non-NixOS hosts."""
    env = _nondefault_env()
    prefix = f"{env} " if env else ""
    return f"sudo {prefix}./scripts/firecracker-netpool.sh up"


def render_nixos_module() -> str:
    """A paste-ready NixOS snippet, derived from the live constants.

    Uses systemd-networkd tap netdevs (so TAPs are only reconfigured
    when this config changes, not torn down on every rebuild — which
    would drop running VMs) and `networking.nftables.tables.<name>`
    (an independent table that coexists with Docker's iptables rules
    rather than replacing the whole ruleset)."""
    slots = all_slots()
    owner = os.environ.get("USER", "youruser")

    netdevs = "\n".join(
        f'''        "10-{s.iface}" = {{
          netdevConfig = {{ Name = "{s.iface}"; Kind = "tap"; }};
          tapConfig = {{ User = "{owner}"; }};
        }};'''
        for s in slots
    )
    networks = "\n".join(
        f'''        "10-{s.iface}" = {{
          matchConfig.Name = "{s.iface}";
          address = [ "{s.host_ip}/31" ];
          networkConfig.ConfigureWithoutCarrier = true;
        }};'''
        for s in slots
    )

    return f"""# bot-bottle Firecracker network pool ({len(slots)} slots, base {ip_base()}).
# Generated by `./cli.py firecracker setup`; owner user = {owner!r}.
{{ ... }}:
{{
  boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

  systemd.network.enable = true;
  systemd.network.netdevs = {{
{netdevs}
  }};
  systemd.network.networks = {{
{networks}
  }};

  # Independent table — does not touch Docker/ufw/firewalld rules.
  # A bottle VM (traffic from a bbfc* TAP) may reach only its own
  # sidecar (DNAT'd from the host TAP IP); everything else is dropped,
  # so there is no egress except through the sidecar proxy.
  networking.nftables.enable = true;
  networking.nftables.tables."{NFT_TABLE}" = {{
    family = "inet";
    content = ''
      chain forward {{
        type filter hook forward priority -10; policy accept;
        iifname != "{IFACE_PREFIX}*" return
        ct state established,related accept
        ct status dnat accept
        drop
      }}
      chain input {{
        type filter hook input priority -10; policy accept;
        iifname != "{IFACE_PREFIX}*" return
        ct state established,related accept
        drop
      }}
    '';
  }};
}}
"""


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
