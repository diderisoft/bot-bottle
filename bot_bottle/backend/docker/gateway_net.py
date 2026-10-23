"""Shared-gateway source-IP allocation for the consolidated docker backend.

In the consolidated model one gateway container serves every bottle over a
single shared docker network, and each agent bottle attaches with a pinned,
deterministic address that the gateway uses as its **attribution key**. This
allocates those addresses from the network's subnet, skipping the reserved
ones — the network address and broadcast (excluded by `hosts()`), docker's
router `.1`, and everything already in use (`taken`: the gateway container
plus every live bottle, which the caller reads from the registry).

Pure `ipaddress` logic — the docker-specific bits (the subnet CIDR, the
gateway container's own address) are gathered by the caller and passed in, so
this stays testable without docker.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable


class NoFreeAddressError(RuntimeError):
    """The shared gateway network's subnet is exhausted — every host address
    is reserved or already assigned to a bottle."""


def next_free_ip(cidr: str, taken: Iterable[str]) -> str:
    """The lowest host address in `cidr` not in `taken` and not docker's
    router (`.1`). `taken` must include the gateway container's own address
    and every live bottle's. Raises `NoFreeAddressError` if the subnet is
    full."""
    net = ipaddress.ip_network(cidr, strict=False)
    reserved = {str(a) for a in taken}
    # Docker assigns the network's first host (.1) to the bridge router; a
    # bottle must never be handed that address.
    reserved.add(str(net.network_address + 1))
    for host in net.hosts():  # hosts() already excludes network + broadcast
        candidate = str(host)
        if candidate not in reserved:
            return candidate
    raise NoFreeAddressError(
        f"no free address in {cidr} ({len(reserved)} reserved/assigned)"
    )


__all__ = ["next_free_ip", "NoFreeAddressError"]
