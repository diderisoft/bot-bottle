"""Per-bottle loopback alias allocation + TSI allowlist
enforcement (PRD 0023, follow-up to PR #74).

After the pivot to host-loopback port-forwards, the smolmachines
TSI allowlist was `127.0.0.1/32` — which meant the agent VM could
reach **any** service bound to macOS's loopback, not just the
bundle's published ports. Real downgrade from the docker
backend's `--internal` network isolation.

This module narrows the allowlist by allocating each bottle a
unique loopback alias (`127.0.0.16` .. `127.0.0.31`). The
bundle's port-forwards bind to that alias, and the alias's /32
is what TSI allows.

**Smolvm 0.8.0 quirk + workaround.** `smolvm machine create
--from <smolmachine> --net --allow-cidr X/32` silently drops the
flag — verified empirically that the agent process's allowlist
ends up `null` in smolvm's persistent state DB (`~/Library/
Application Support/smolvm/server/smolvm.db`, `vms` table,
`data` BLOB), and the booted VM reaches all of `127.0.0.0/8`
regardless of what we passed. Workaround: after machine_create,
open the SQLite DB and patch the row's `allowed_cidrs` field
directly. Smolvm reads the DB at machine_start, so the patched
value takes effect on boot. Tested: enforcement is real — the
guest's connect to a non-allowlisted IP fails with `Permission
denied`. Other paths we tried (machine update, stop-edit-
agent.config.json-restart, --smolfile, --image localhost:N/...)
were dead ends.

macOS only configures `127.0.0.1` on `lo0` by default; the
additional aliases require `sudo ifconfig lo0 alias`. We lazily
sudo-add the missing pool on first use per boot — the aliases
persist on `lo0` until reboot, so subsequent launches don't
prompt.

Linux native daemons share the host's network namespace; the
whole `127.0.0.0/8` is reachable by default and aliases are
unnecessary. The pool logic detects native-Linux and skips sudo
entirely; the DB patch is also gated on macOS.

Allocation is coordinated by inspecting running bundle
containers' published host IPs — each bottle's bundle owns the
alias appearing in its port bindings. The lowest-numbered free
alias gets handed to a new bottle."""

from __future__ import annotations

import fcntl
import json
import platform
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

from ...log import die, info


# smolvm's persistent VM state on macOS — a SQLite DB whose `vms`
# table holds one JSON BLOB per machine. The Linux path is
# different, but smolmachines is macOS-only in v1 (PRD 0023) so
# we hard-code this. If the file moves under us we'll see a
# clear FileNotFoundError; not worth defensive cross-platform
# detection until the backend actually needs Linux.
_SMOLVM_DB_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "smolvm"
    / "server"
    / "smolvm.db"
)


# Sixteen aliases by default. Tunable for hosts that want more
# concurrent bottles (each bottle reserves one alias for its
# bundle bringup). The range is chosen to avoid the reserved
# 127.0.0.1/2/3 ports (1 is the default, 2 is sometimes used by
# CUPS, 3 by other macOS services) and stay well clear of
# 127.0.0.53 (systemd-resolved) and 127.0.0.54 (libvirt).
_POOL_START = 16
_POOL_END = 31  # inclusive


# File lock that serialises concurrent allocate() calls so two
# simultaneous launches can't read the same docker state and claim
# the same alias. Narrowed to the allocate() call itself; docker run
# runs after the lock is released. Once the container is running it
# appears in docker state and future allocate() calls will see it.
_ALLOC_LOCK_PATH = Path.home() / ".cache" / "bot-bottle" / "smolmachines.lock"


# Loopback aliases pool: 127.0.0.<start>..127.0.0.<end>.
def _pool_addresses() -> list[str]:
    return [f"127.0.0.{i}" for i in range(_POOL_START, _POOL_END + 1)]


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def ensure_pool() -> None:
    """Make sure each address in the pool is up on `lo0`. Lazily
    runs `sudo ifconfig lo0 alias <ip>/32 up` for missing entries
    (sudo prompts once, then the aliases persist on lo0 until
    reboot). No-op on non-macOS hosts."""
    if not _is_macos():
        return
    missing = [ip for ip in _pool_addresses() if not _alias_present(ip)]
    if not missing:
        return
    info(
        f"smolmachines needs {len(missing)} loopback alias(es) on lo0 "
        f"({', '.join(missing[:3])}{', ...' if len(missing) > 3 else ''}) "
        f"to scope per-bottle TSI allowlists. sudo will prompt once; "
        f"aliases persist until reboot."
    )
    for ip in missing:
        result = subprocess.run(
            ["sudo", "-p", "bot-bottle (loopback alias): ",
             "ifconfig", "lo0", "alias", f"{ip}/32", "up"],
            check=False,
        )
        if result.returncode != 0:
            die(
                f"sudo ifconfig lo0 alias {ip} failed (exit "
                f"{result.returncode}). Re-run with sudo available, "
                f"or add manually: sudo ifconfig lo0 alias {ip}/32 up"
            )


def force_allowlist(machine_name: str, allowed_cidrs: list[str]) -> None:
    """Patch smolvm's persistent VM-state DB to set the machine's
    `allowed_cidrs` to the given list. Workaround for smolvm
    0.8.0's silent-drop of `--allow-cidr` when used with `--from`.

    Must run AFTER `smolvm machine create` (the row has to
    exist) and BEFORE `smolvm machine start` (smolvm reads the
    row on start; in-flight VMs don't pick up changes). Once
    smolvm honors the CLI flag upstream this whole function is
    redundant — flag-respecting create + remove this call from
    launch.

    No-op on non-macOS — the DB path differs and the Linux
    smolmachines code path isn't exercised in v1."""
    if not _is_macos():
        return
    if not _SMOLVM_DB_PATH.is_file():
        die(
            f"smolvm state DB not found at {_SMOLVM_DB_PATH}. "
            f"smolvm 0.8.0 expected? `smolvm --version` to check."
        )
    con = sqlite3.connect(str(_SMOLVM_DB_PATH))
    try:
        cur = con.cursor()
        row = cur.execute(
            "SELECT data FROM vms WHERE name = ?", (machine_name,),
        ).fetchone()
        if row is None:
            die(
                f"smolvm DB has no row for machine {machine_name!r} — "
                f"machine_create must run before force_allowlist."
            )
        cfg = json.loads(row[0])
        cfg["allowed_cidrs"] = list(allowed_cidrs)
        # Write as BLOB (the column type smolvm uses) — passing a
        # plain str makes sqlite store it as Text and smolvm then
        # fails to read it.
        cur.execute(
            "UPDATE vms SET data = ? WHERE name = ?",
            (sqlite3.Binary(json.dumps(cfg).encode()), machine_name),
        )
        con.commit()
    finally:
        con.close()


def allocate(slug: str) -> str:
    """Pick the lowest-numbered alias from the pool not already
    in use by a running smolmachines bundle. Bails when the pool
    is exhausted — the caller should report the limit to the
    operator. `slug` is logged for traceability; not otherwise
    used (no on-disk reservation, allocation is purely
    docker-state-driven).

    On non-macOS the whole `127.0.0.0/8` is loopback by default;
    `127.0.0.1` is fine to share and we skip the alias dance.
    This still returns a deterministic address so launch.py's
    callers don't have to branch on platform.

    An exclusive file lock serialises concurrent calls so two
    simultaneous launches don't read the same docker state and
    claim the same alias."""
    if not _is_macos():
        return "127.0.0.1"
    _ALLOC_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ALLOC_LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        return _allocate_locked()


def _allocate_locked() -> str:
    in_use = _aliases_in_use()
    for ip in _pool_addresses():
        if ip not in in_use:
            return ip
    die(
        f"smolmachines loopback alias pool exhausted "
        f"({_POOL_END - _POOL_START + 1} aliases, all in use). "
        f"Stop a running bottle (`smolvm machine ls --json`) or "
        f"raise _POOL_END in loopback_alias.py."
    )
    return ""  # unreachable; die() never returns


def _alias_present(ip: str) -> bool:
    """True iff `ifconfig lo0` shows `<ip>` as an inet address.
    Exact-match — `127.0.0.1` shouldn't match `127.0.0.16`."""
    result = subprocess.run(
        ["/sbin/ifconfig", "lo0"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False
    pattern = re.compile(rf"\binet {re.escape(ip)}\b")
    return bool(pattern.search(result.stdout or ""))


def _aliases_in_use() -> set[str]:
    """Aliases already bound by another smolmachines bundle's
    published-port mappings. We inspect every container whose
    name matches the smolmachines bundle prefix and pull the
    `HostIp` out of its port bindings."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}",
         "--filter", "name=bot-bottle-sidecars-"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return set()
    names = [n.strip() for n in (result.stdout or "").splitlines() if n.strip()]
    in_use: set[str] = set()
    for name in names:
        in_use.update(_host_ips_for_container(name))
    return in_use


def _host_ips_for_container(name: str) -> Iterable[str]:
    """Yield the `HostIp` values across all port bindings on
    container `name`. A bundle binds three or four ports and
    they all share the same HostIp, so callers can take any."""
    result = subprocess.run(
        ["docker", "inspect", name,
         "--format", "{{json .HostConfig.PortBindings}}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return ()
    try:
        bindings = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ()
    seen: set[str] = set()
    for _port, mappings in (bindings or {}).items():
        for m in mappings or []:
            host_ip = m.get("HostIp") or ""
            if host_ip:
                seen.add(host_ip)
    return seen


__all__ = ["allocate", "ensure_pool", "force_allowlist"]
