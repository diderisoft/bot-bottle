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
import shutil
import sys
from pathlib import Path

from . import netpool
from . import util


_FC_RELEASES = "https://github.com/firecracker-microvm/firecracker/releases"


def _owner() -> str:
    return os.environ.get("USER", "youruser")


def _module_path() -> str:
    """Absolute path to the importable NixOS module in this checkout."""
    return str(Path(__file__).resolve().parents[3] / "nix" / "firecracker-netpool.nix")


def _print_prereqs() -> None:
    """The firecracker binary + KVM + guest artifacts, shown before the
    privileged network-pool step so operators see the full picture."""
    fc = shutil.which("firecracker")
    if fc:
        sys.stderr.write(f"1) firecracker binary: found ({fc}).\n")
    else:
        sys.stderr.write(
            "1) firecracker binary: NOT found on PATH. Install a release binary "
            "and put it on PATH:\n"
            f"     {_FC_RELEASES}\n"
            "   e.g.: download firecracker-vX.Y.Z-$(uname -m).tgz, extract, and\n"
            "     install -m755 release-*/firecracker-* ~/.local/bin/firecracker\n"
            "   (NixOS: not packaged as a user binary — fetch the release, pin\n"
            "    the version, and add it to PATH.)\n"
        )
    if util.is_host_capable():
        sys.stderr.write("   KVM: /dev/kvm present.\n")
    else:
        sys.stderr.write(
            "   KVM: /dev/kvm missing/unusable — load kvm-intel/kvm-amd, enable\n"
            "   virtualization in firmware, and add your user to the `kvm` group.\n"
        )
    sys.stderr.write(
        "   Guest artifacts: a kernel (BOT_BOTTLE_FC_KERNEL) and static dropbear\n"
        "   (BOT_BOTTLE_FC_DROPBEAR) must be cached, and `mke2fs` (e2fsprogs) is\n"
        "   needed to build the rootfs.\n\n"
    )


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
    sys.stderr.write("Firecracker backend — one-time host setup.\n\n")
    _print_prereqs()
    slots = netpool.all_slots()
    sys.stderr.write(
        f"2) network pool: {len(slots)} slots "
        f"({slots[0].iface}..{slots[-1].iface}), base {netpool.ip_base()} — "
        f"TAP devices + nft isolation table, privileged (needs root once).\n\n"
    )
    _warn_overlaps()
    if _is_nixos():
        sys.stderr.write(
            "Detected NixOS. Import the module — it is NON-INVASIVE: it does "
            "not flip networking.nftables.enable or systemd.network.enable, so "
            "your existing (iptables) firewall and Docker are untouched. A "
            "systemd oneshot brings the pool up alongside them.\n\n"
            "  # flake users:\n"
            "  inputs.bot-bottle.url = \"git+ssh://<your-bot-bottle-remote>\";\n"
            "  imports = [ inputs.bot-bottle.nixosModules.firecracker-netpool ];\n"
            "  # channel (non-flake) users — import the file directly:\n"
            f"  imports = [ {_module_path()} ];\n\n"
            f"  services.bot-bottle-firecracker = {{ enable = true; owner = \"{_owner()}\"; }};\n\n"
            "Then `nixos-rebuild switch`.\n"
        )
    else:
        sys.stderr.write("Run the one-time setup as root:\n\n")
        sys.stdout.write(netpool.render_shell_setup() + "\n")
        sys.stderr.write(
            "\n(On NixOS, use the declarative module instead — this host "
            "was not detected as NixOS.)\n"
        )
    return 0


def teardown() -> int:
    slots = netpool.all_slots()
    sys.stderr.write(
        f"Undo the Firecracker network pool ({len(slots)} slots, base "
        f"{netpool.ip_base()}) — a privileged, one-time operation.\n\n"
    )
    if _is_nixos():
        sys.stderr.write(
            "On NixOS: set `services.bot-bottle-firecracker.enable = false;` "
            "(or drop the module import) and `nixos-rebuild switch`. The TAP "
            "netdevs and nft table are removed declaratively.\n\n"
            "To tear down imperatively before a rebuild (does not persist):\n\n"
        )
    else:
        sys.stderr.write("Run the teardown as root:\n\n")
    sys.stdout.write("sudo ./scripts/firecracker-netpool.sh down\n")
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
