"""Per-bottle loopback alias allocation (PRD 0023, follow-up to
the Docker-Desktop fix in PR #74).

After the pivot to host-loopback port-forwards, the smolmachines
TSI allowlist was `127.0.0.1/32` — which meant the agent VM could
reach **any** service bound to macOS's loopback, not just the
bundle's published ports. That's a real downgrade from the
docker backend's `--internal` network isolation.

This module is the host-side half of the eventual fix: allocate
each bottle a unique loopback alias (`127.0.0.16` .. `127.0.0.31`
by default), bind the bundle's port-forwards to that alias, and
pass the alias's /32 as smolvm's `--allow-cidr`. If TSI enforced
the allowlist, the agent could only reach its own bundle.

**Upstream block, smolvm 0.8.0:** verified empirically that
`smolvm machine create --from <smolmachine> --net --allow-cidr
X/32` silently drops the allowlist. The persisted
`agent.config.json` shows `allowed_cidrs: null`, and the running
VM can reach any host loopback service regardless of the
flag. `machine update --allow-cidr` doesn't exist; stop-edit-
start of `agent.config.json` doesn't work (the file is removed
on stop); `--smolfile` is mutually exclusive with `--from`. So
the alias scoping infrastructure lives here, ready, but the
TSI enforcement is blocked on a smolvm upstream fix. Until that
lands, the agent can still reach the whole `127.0.0.0/8`. The
README + gitea issue #75 spell this out.

macOS only configures `127.0.0.1` on `lo0` by default; the
additional aliases require `sudo ifconfig lo0 alias`. We lazily
sudo-add the missing pool on first use per boot — the aliases
persist on `lo0` until reboot, so subsequent launches don't
prompt.

Linux native daemons share the host's network namespace; the
whole `127.0.0.0/8` is reachable by default and aliases are
unnecessary. The pool logic detects native-Linux and skips sudo
entirely.

Allocation is coordinated by inspecting running bundle
containers' published host IPs — each bottle's bundle owns the
alias appearing in its port bindings. The lowest-numbered free
alias gets handed to a new bottle."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from typing import Iterable

from ...log import die, info


# Sixteen aliases by default. Tunable for hosts that want more
# concurrent bottles (each bottle reserves one alias for its
# bundle bringup). The range is chosen to avoid the reserved
# 127.0.0.1/2/3 ports (1 is the default, 2 is sometimes used by
# CUPS, 3 by other macOS services) and stay well clear of
# 127.0.0.53 (systemd-resolved) and 127.0.0.54 (libvirt).
_POOL_START = 16
_POOL_END = 31  # inclusive


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
            ["sudo", "-p", "claude-bottle (loopback alias): ",
             "ifconfig", "lo0", "alias", f"{ip}/32", "up"],
            check=False,
        )
        if result.returncode != 0:
            die(
                f"sudo ifconfig lo0 alias {ip} failed (exit "
                f"{result.returncode}). Re-run with sudo available, "
                f"or add manually: sudo ifconfig lo0 alias {ip}/32 up"
            )


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
    callers don't have to branch on platform."""
    if not _is_macos():
        return "127.0.0.1"
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
         "--filter", "name=claude-bottle-sidecars-"],
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


__all__ = ["allocate", "ensure_pool"]
