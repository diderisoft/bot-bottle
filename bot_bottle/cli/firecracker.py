"""`firecracker` CLI command — one-time network setup helper.

`./cli.py firecracker setup` prints the host-appropriate config for the
privileged, one-time network pool (TAP devices + isolation nftables
table) the Firecracker backend needs. On NixOS it prints a declarative
module to paste into your config (imperative rules don't survive
nixos-rebuild); elsewhere it prints the sudo command for the bundled
setup script. `./cli.py firecracker status` reports what's present.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..backend.firecracker import netpool


def _is_nixos() -> bool:
    if Path("/etc/NIXOS").exists():
        return True
    try:
        return "ID=nixos" in Path("/etc/os-release").read_text()
    except OSError:
        return False


def _cmd_setup() -> int:
    slots = netpool.all_slots()
    sys.stderr.write(
        f"Firecracker network pool: {len(slots)} slots "
        f"({slots[0].iface}..{slots[-1].iface}), base {netpool.ip_base()}.\n"
        f"This is a one-time privileged setup (needs root once).\n\n"
    )
    if _is_nixos():
        sys.stderr.write(
            "Detected NixOS. Paste this module into your configuration and "
            "`nixos-rebuild switch` (imperative rules would not survive a "
            "rebuild):\n\n"
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


def _cmd_status() -> int:
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
    if not ok:
        sys.stderr.write("\nRun: ./cli.py firecracker setup\n")
    return 0 if ok else 1


def cmd_firecracker(args: list[str]) -> int:
    sub = args[0] if args else ""
    if sub == "setup":
        return _cmd_setup()
    if sub == "status":
        return _cmd_status()
    sys.stderr.write("usage: firecracker {setup|status}\n")
    return 2
