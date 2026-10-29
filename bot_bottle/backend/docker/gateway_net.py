"""Address allocation for docker agents. Pure `ipaddress` logic.

Each agent gets its own `--internal` network, a /29 carved from
`AGENT_SUBNET_POOL`, holding only docker's router (`.1`), the gateway (`.2`)
and the agent (`.3`). Agents share no network, so they can't reach each
other. The agent's address is the key the gateway attributes it by; the
subnets never overlap, so it's unique.

`next_free_ip` allocates on the shared gateway network, which bottles
launched before per-agent networks may still be on.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator


# Where per-agent subnets come from: 512 /29s, below the shared gateway
# network's default `10.242.255.0/24`.
AGENT_SUBNET_POOL = "10.242.0.0/20"
AGENT_SUBNET_PREFIX = 29


class NoFreeAddressError(RuntimeError):
    """The gateway network's subnet, or the agent subnet pool, is exhausted."""


def next_free_ip(cidr: str, taken: Iterable[str]) -> str:
    """The lowest host address in `cidr` not in `taken` and not docker's router
    (`.1`). `taken` must include the gateway and every live bottle. Raises
    `NoFreeAddressError` when full."""
    net = ipaddress.ip_network(cidr, strict=False)
    reserved = {str(a) for a in taken}
    # Docker gives `.1` to the bridge router.
    reserved.add(str(net.network_address + 1))
    for host in net.hosts():  # excludes network and broadcast
        candidate = str(host)
        if candidate not in reserved:
            return candidate
    raise NoFreeAddressError(
        f"no free address in {cidr} ({len(reserved)} reserved/assigned)"
    )


def free_agent_subnets(taken: Iterable[str], pool: str = AGENT_SUBNET_POOL) -> Iterator[str]:
    """Each /29 in `pool` that overlaps none of the `taken` subnets (every
    docker network's), lowest first."""
    used = [ipaddress.ip_network(c, strict=False) for c in taken]
    used4 = [n for n in used if isinstance(n, ipaddress.IPv4Network)]
    for subnet in ipaddress.IPv4Network(pool).subnets(new_prefix=AGENT_SUBNET_PREFIX):
        if not any(subnet.overlaps(n) for n in used4):
            yield str(subnet)


def agent_subnet_addresses(cidr: str) -> tuple[str, str]:
    """(gateway, agent) addresses on a per-agent subnet: `.2` and `.3`, after
    docker's `.1` router."""
    base = ipaddress.ip_network(cidr, strict=False).network_address
    return str(base + 2), str(base + 3)


__all__ = [
    "AGENT_SUBNET_POOL",
    "AGENT_SUBNET_PREFIX",
    "NoFreeAddressError",
    "agent_subnet_addresses",
    "free_agent_subnets",
    "next_free_ip",
]
