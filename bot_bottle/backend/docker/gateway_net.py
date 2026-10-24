"""Source-IP allocation on the shared docker gateway network. Each agent gets a
pinned address the gateway attributes it by. Pure `ipaddress` logic.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable


class NoFreeAddressError(RuntimeError):
    """The gateway network's subnet is exhausted."""


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


__all__ = ["next_free_ip", "NoFreeAddressError"]
