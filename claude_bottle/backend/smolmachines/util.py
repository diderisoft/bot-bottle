"""Slug / preflight / subnet helpers for the smolmachines backend
(PRD 0023). Kept in its own module so the renderers can be
unit-tested without importing the docker subprocess paths."""

from __future__ import annotations

import hashlib
import shutil

from ...log import die


def smolmachines_preflight() -> None:
    """Ensure `smolvm` is on PATH before the launch flow runs.
    Called from `_resolve_plan`; gives the operator a clear
    install pointer rather than a cryptic FileNotFoundError
    later. `gvproxy` is no longer required — see the PRD's design
    pivot section."""
    if shutil.which("smolvm") is not None:
        return
    die(
        "CLAUDE_BOTTLE_BACKEND=smolmachines requires `smolvm` on "
        "PATH. Install with: "
        "curl -sSL https://smolmachines.com/install.sh | sh"
    )


def smolmachines_bundle_subnet(slug: str) -> tuple[str, str, str]:
    """Derive a per-bottle docker subnet + gateway IP + bundle IP
    from the slug.

    Returns `(subnet_cidr, gateway_ip, bundle_ip)`. The third
    octet comes from SHA-256 of the slug mod 254 (skipping 17 to
    avoid the docker-default bridge), so parallel bottles get
    distinct /24s and `resume` reuses the same /24. The bundle
    container always lands at `.2`; gateway is `.1`; the smolvm
    Smolfile's `allow_cidrs` is `<bundle_ip>/32`."""
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    octet = (digest[0] % 254) + 1
    # Skip the docker-default bridge to dodge the most common
    # collision (operators with `docker0` at 172.17.x.x or a
    # 192.168.17.x VPN client).
    if octet == 17:
        octet = 18
    subnet = f"192.168.{octet}.0/24"
    gateway = f"192.168.{octet}.1"
    bundle_ip = f"192.168.{octet}.2"
    return subnet, gateway, bundle_ip
