"""Slug / preflight / subnet helpers for the smolmachines backend
(PRD 0023). Kept in its own module so the renderers can be
unit-tested without importing the docker subprocess paths."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil

from ...log import die

# libkrun's Linux backend drives the guest through KVM, so the host
# must expose `/dev/kvm` and the invoking user must be able to open
# it. macOS uses Hypervisor.framework and needs no device node.
_KVM_DEVICE = "/dev/kvm"


def smolmachines_preflight() -> None:
    """Ensure the host can run the smolmachines backend before the
    launch flow starts. Called from `_resolve_plan`; surfaces a
    clear, actionable error instead of a cryptic `smolvm` failure
    deep in launch.

    Checks `smolvm` is on PATH (both platforms) and, on Linux,
    that `/dev/kvm` exists and is accessible. `gvproxy` is no
    longer required — see the PRD's design pivot section."""
    if shutil.which("smolvm") is None:
        die(
            "BOT_BOTTLE_BACKEND=smolmachines requires `smolvm` on "
            "PATH. Install with: "
            "curl -sSL https://smolmachines.com/install.sh | sh. "
            "To use the legacy Docker backend instead, set "
            "BOT_BOTTLE_BACKEND=docker or pass --backend=docker."
        )
    if platform.system() == "Linux":
        _preflight_kvm()


def _preflight_kvm() -> None:
    """Linux-only: libkrun needs `/dev/kvm`. Distinguish 'KVM not
    enabled' from 'no permission' so the operator knows which to
    fix."""
    if not os.path.exists(_KVM_DEVICE):
        die(
            f"BOT_BOTTLE_BACKEND=smolmachines needs {_KVM_DEVICE} on "
            "Linux but it is missing. Enable KVM: load the kvm-intel "
            "or kvm-amd kernel module (and confirm virtualization is "
            "enabled in BIOS/firmware). To use the legacy Docker "
            "backend instead, set BOT_BOTTLE_BACKEND=docker."
        )
    if not os.access(_KVM_DEVICE, os.R_OK | os.W_OK):
        die(
            f"{_KVM_DEVICE} exists but is not readable/writable by the "
            "current user. Add your user to the `kvm` group "
            "(`sudo usermod -aG kvm \"$USER\"`) and re-login, or run "
            "with access to the device."
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
