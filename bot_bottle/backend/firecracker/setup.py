"""Host setup + status for the Firecracker backend.

`setup()` prints the host-appropriate config for the privileged,
one-time network pool (TAP devices + isolation nftables table) the
backend needs. On NixOS it points at the flake module (and prints a
paste-able fallback); elsewhere it prints the sudo command for the
bundled setup script. `status()` reports what's present, including
whether the pool range collides with an existing route.

Called through `FirecrackerBottleBackend.setup` / `.status`, which the
generic `./cli.py backend {setup,status}` command dispatches to.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import netpool


def _owner() -> str:
    return os.environ.get("USER", "youruser")


def _is_nixos() -> bool:
    if Path("/etc/NIXOS").exists():
        return True
    try:
        return "ID=nixos" in Path("/etc/os-release").read_text()
    except OSError:
        return False


def _warn_overlaps() -> None:
    """Warn if the chosen pool range collides with an existing host route
    (Tailscale CGNAT peer, a docker/libvirt bridge, the LAN…)."""
    conflicts = netpool.overlapping_routes()
    if not conflicts:
        return
    detail = "\n".join(f"    {c.dst} dev {c.dev}" for c in conflicts)
    sys.stderr.write(
        f"WARNING: pool range (base {netpool.ip_base()}, "
        f"{netpool.pool_size()} slots) overlaps existing routes:\n"
        f"{detail}\n"
        "Set BOT_BOTTLE_FC_IP_BASE to a free range before setup, or the "
        "pool may shadow / be shadowed by the above.\n\n"
    )


def setup() -> int:
    slots = netpool.all_slots()
    sys.stderr.write(
        f"Firecracker network pool: {len(slots)} slots "
        f"({slots[0].iface}..{slots[-1].iface}), base {netpool.ip_base()}.\n"
        f"This is a one-time privileged setup (needs root once).\n\n"
    )
    _warn_overlaps()
    if _is_nixos():
        sys.stderr.write(
            "Detected NixOS. Preferred: consume the flake module (versioned, "
            "no copy-paste drift):\n\n"
            "  # flake inputs (point at wherever you host bot-bottle):\n"
            "  inputs.bot-bottle.url = \"git+ssh://<your-bot-bottle-remote>\";\n"
            "  # host module:\n"
            "  imports = [ inputs.bot-bottle.nixosModules.firecracker-netpool ];\n"
            f"  services.bot-bottle-firecracker = {{ enable = true; owner = \"{_owner()}\"; }};\n\n"
            "Then `nixos-rebuild switch`. Channel (non-flake) users can "
            "`imports = [ <bot-bottle>/nix/firecracker-netpool.nix ];` instead.\n\n"
            "Fallback — paste this generated module directly:\n\n"
        )
        sys.stdout.write(netpool.render_nixos_module())
    else:
        sys.stderr.write("Run the one-time setup as root:\n\n")
        sys.stdout.write(netpool.render_shell_setup() + "\n")
        sys.stderr.write(
            "\n(On NixOS, use the declarative module instead — this host "
            "was not detected as NixOS.)\n"
        )
    return 0


def status() -> int:
    ok = True
    if netpool.nft_table_present():
        sys.stderr.write(f"nft table inet {netpool.NFT_TABLE}: present\n")
    else:
        sys.stderr.write(f"nft table inet {netpool.NFT_TABLE}: MISSING\n")
        ok = False
    missing = netpool.missing_taps()
    total = netpool.pool_size()
    if missing:
        sys.stderr.write(f"TAP pool: {total - len(missing)}/{total} present "
                         f"(missing: {', '.join(missing)})\n")
        ok = False
    else:
        sys.stderr.write(f"TAP pool: {total}/{total} present\n")
    conflicts = netpool.overlapping_routes()
    if conflicts:
        detail = ", ".join(f"{c.dst} dev {c.dev}" for c in conflicts)
        sys.stderr.write(f"range overlap: base {netpool.ip_base()} CLASHES "
                         f"with {detail}\n")
        ok = False
    else:
        sys.stderr.write(f"range overlap: none (base {netpool.ip_base()})\n")
    if not ok:
        sys.stderr.write("\nRun: ./cli.py backend setup --backend=firecracker\n")
    return 0 if ok else 1
