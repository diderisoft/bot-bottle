"""Firecracker setup and status. `setup()` prints the one-time network pool
config: the flake module on NixOS (with a fallback), else the sudo command for
the bundled script. `status()` reports what's present, including route
overlaps.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ... import resources
from .. import container_engine
from ...contract.hostctl import Settings
from . import netpool
from . import util

# KVM_GET_API_VERSION = _IO(0xAE, 0x00): the cheapest proof of KVM access.
_KVM_GET_API_VERSION = 0xAE00


_FC_RELEASES = "https://github.com/firecracker-microvm/firecracker/releases"
_UNIT_PATH = Path("/etc/systemd/system") / netpool.SYSTEMD_UNIT


def _owner() -> str:
    # Under sudo, SUDO_USER is the real user, who must own the TAPs.
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "youruser"


def _has_systemd() -> bool:
    return Path("/run/systemd/system").is_dir()


def _module_path() -> str:
    """The NixOS module's path (checkout or wheel)."""
    return str(resources.nix_netpool_module())


def _script_path() -> str:
    """The bundled setup script's path (checkout or wheel)."""
    return str(resources.netpool_script())


def _print_prereqs() -> None:
    """Print the firecracker binary, KVM and guest artifact prerequisites."""
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
        "   Guest artifacts: the kernel and static dropbear are downloaded into\n"
        "   the cache (or set BOT_BOTTLE_FC_KERNEL / BOT_BOTTLE_FC_DROPBEAR), and\n"
        "   `mke2fs` (e2fsprogs) is needed to build the rootfs.\n\n"
    )


def _is_nixos() -> bool:
    if Path("/etc/NIXOS").exists():
        return True
    try:
        return "ID=nixos" in Path("/etc/os-release").read_text()
    except OSError:
        return False


def _warn_overlaps(pool: netpool.NetPool) -> None:
    """Warn if the pool range overlaps an existing host route."""
    conflicts = pool.overlapping_routes()
    if not conflicts:
        return
    detail = "\n".join(f"    {c.dst} dev {c.dev}" for c in conflicts)
    sys.stderr.write(
        f"WARNING: pool range (base {pool.ip_base}, "
        f"{pool.pool_size} slots) overlaps existing routes:\n"
        f"{detail}\n"
        "Set BOT_BOTTLE_FC_IP_BASE to a free range before setup, or the "
        "pool may shadow / be shadowed by the above.\n\n"
    )


def setup(settings: Settings) -> int:
    pool = netpool.host_pool(settings.infra)
    sys.stderr.write("Firecracker backend — one-time host setup.\n\n")
    _print_prereqs()
    slots = pool.all_slots()
    sys.stderr.write(
        f"2) network pool: {len(slots)} slots "
        f"({slots[0].iface}..{slots[-1].iface}), base {pool.ip_base}, plus "
        f"{pool.env_slot_count} named-environment slices of the same size — "
        f"TAP devices + nft isolation tables, privileged (needs root once).\n\n"
    )
    _warn_overlaps(pool)
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
    elif _has_systemd():
        _setup_systemd(pool)
    else:
        sys.stderr.write(
            "No systemd detected. Run the one-time bring-up as root (and add "
            "your own boot persistence — e.g. an OpenRC/runit service):\n\n"
        )
        sys.stdout.write(netpool.render_shell_setup(pool) + "\n")
    return 0


def root_setup(settings: Settings) -> str | None:
    """The network pool's systemd unit, when it isn't installed as setup would
    write it. NixOS takes a module and a host without systemd its own
    script, neither of which sudo can do for you."""
    if os.geteuid() == 0 or _is_nixos() or not _has_systemd():
        return None
    unit = netpool.render_systemd_unit(netpool.host_pool(settings.infra), _owner(), _script_path())
    try:
        if _UNIT_PATH.read_text(encoding="utf-8") == unit:
            return None
    except OSError:
        pass
    return f"install the network pool ({netpool.SYSTEMD_UNIT}: TAP devices, nft tables)"


def _setup_systemd(pool: netpool.NetPool) -> None:
    """Install the pool as a persistent systemd unit: directly when root,
    otherwise print a copy-paste block."""
    unit = netpool.render_systemd_unit(pool, _owner(), _script_path())
    sys.stderr.write(
        f"Persistent install (systemd — same on every systemd distro). Needs "
        f"`nft` (nftables) and `ip` (iproute2); install via your package "
        f"manager if `doctor` reports them missing.\n\n"
    )
    if os.geteuid() == 0:
        _UNIT_PATH.write_text(unit)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        rc = subprocess.run(
            ["systemctl", "enable", "--now", netpool.SYSTEMD_UNIT], check=False,
        ).returncode
        if rc == 0:
            sys.stderr.write(
                f"Installed and started {netpool.SYSTEMD_UNIT}. Verify with "
                f"`bot-bottle doctor`.\n"
            )
        else:
            sys.stderr.write(
                f"Wrote {_UNIT_PATH} but `systemctl enable --now` failed — "
                f"check `systemctl status {netpool.SYSTEMD_UNIT}`.\n"
            )
        return
    sys.stderr.write(
        "Install the unit (one copy-paste; enables it on boot too):\n\n"
    )
    sys.stdout.write(
        f"sudo tee {_UNIT_PATH} >/dev/null <<'UNIT'\n"
        f"{unit}"
        f"UNIT\n"
        f"sudo systemctl daemon-reload\n"
        f"sudo systemctl enable --now {netpool.SYSTEMD_UNIT}\n"
    )
    sys.stderr.write(
        f"\n(Or re-run this as root to install it directly: "
        f"sudo bot-bottle install --backend=firecracker)\n"
    )


def teardown(settings: Settings) -> int:
    pool = netpool.host_pool(settings.infra)
    slots = pool.all_slots()
    sys.stderr.write(
        f"Undo the Firecracker network pool ({len(slots)} slots, base "
        f"{pool.ip_base}) — a privileged operation.\n\n"
    )
    if _is_nixos():
        sys.stderr.write(
            "On NixOS: set `services.bot-bottle-firecracker.enable = false;` "
            "(or drop the module import) and `nixos-rebuild switch`. The TAP "
            "netdevs and nft table are removed declaratively.\n\n"
            "To tear down imperatively before a rebuild (does not persist):\n\n"
        )
        sys.stdout.write("sudo ./scripts/firecracker-netpool.sh down\n")
        return 0
    if _has_systemd():
        if os.geteuid() == 0:
            subprocess.run(
                ["systemctl", "disable", "--now", netpool.SYSTEMD_UNIT],
                check=False,
            )
            _UNIT_PATH.unlink(missing_ok=True)
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            sys.stderr.write(
                f"Stopped, disabled, and removed {netpool.SYSTEMD_UNIT}.\n"
            )
        else:
            sys.stderr.write("Remove the persistent unit (one copy-paste):\n\n")
            sys.stdout.write(
                f"sudo systemctl disable --now {netpool.SYSTEMD_UNIT}\n"
                f"sudo rm -f {_UNIT_PATH}\n"
                f"sudo systemctl daemon-reload\n"
            )
        return 0
    sys.stderr.write("Run the teardown as root:\n\n")
    sys.stdout.write("sudo ./scripts/firecracker-netpool.sh down\n")
    return 0


def _firecracker_binary_ok() -> bool:
    """True iff `firecracker --version` runs and exits 0."""
    if shutil.which("firecracker") is None:
        return False
    try:
        return subprocess.run(
            ["firecracker", "--version"],
            capture_output=True, check=False, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kvm_accessible() -> bool:
    """True iff /dev/kvm opens read-write and answers KVM_GET_API_VERSION. VM
    creation needs write access."""
    if not os.path.exists(util._KVM_DEVICE):
        return False
    try:
        fd = os.open(util._KVM_DEVICE, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, _KVM_GET_API_VERSION)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def status(settings: Settings) -> int:
    pool = netpool.from_settings(settings.infra)
    # Ready means what the launch preflight requires: binary, /dev/kvm, TAP
    # pool, no overlap. An unconfirmable nft table is reported but not fatal;
    # the post-boot probe is authoritative.
    ok = True
    if _firecracker_binary_ok():
        sys.stderr.write(f"firecracker binary: ok ({shutil.which('firecracker')})\n")
    else:
        fc_path = shutil.which("firecracker")
        if fc_path is None:
            sys.stderr.write("firecracker binary: NOT found on PATH\n")
        else:
            sys.stderr.write(
                f"firecracker binary: found ({fc_path}) but `--version` failed\n"
            )
        ok = False
    if _kvm_accessible():
        sys.stderr.write(f"KVM: {util._KVM_DEVICE} accessible\n")
    else:
        if not os.path.exists(util._KVM_DEVICE):
            sys.stderr.write(f"KVM: {util._KVM_DEVICE} not present\n")
        else:
            sys.stderr.write(
                f"KVM: {util._KVM_DEVICE} not accessible (open/ioctl failed)\n"
            )
        ok = False
    kernel = util.kernel_path(settings.infra)
    if kernel.is_file():
        sys.stderr.write(f"guest kernel: {kernel}\n")
    else:
        sys.stderr.write(
            f"guest kernel: NOT found at {kernel} "
            f"(set BOT_BOTTLE_FC_KERNEL or cache a vmlinux there)\n"
        )
        ok = False
    dropbear = util.dropbear_path(settings.infra)
    if dropbear.is_file():
        sys.stderr.write(f"dropbear: {dropbear}\n")
    else:
        sys.stderr.write(
            f"dropbear: NOT found at {dropbear} "
            f"(set BOT_BOTTLE_FC_DROPBEAR or cache a static binary)\n"
        )
        ok = False
    mke2fs = shutil.which("mke2fs")
    if mke2fs is not None:
        sys.stderr.write(f"mke2fs: {mke2fs}\n")
    else:
        sys.stderr.write("mke2fs: NOT found on PATH (install e2fsprogs)\n")
        ok = False
    # debugfs, e2fsck and resize2fs prepare pulled images rootless.
    missing_e2fs = [t for t in ("debugfs", "e2fsck", "resize2fs") if shutil.which(t) is None]
    if missing_e2fs:
        sys.stderr.write(f"e2fsprogs: missing {', '.join(missing_e2fs)} (install e2fsprogs)\n")
        ok = False
    else:
        sys.stderr.write("e2fsprogs: debugfs, e2fsck, resize2fs present\n")
    # Optional: overlays build on the host when an engine is available.
    try:
        engine = container_engine.host_container_engine(settings.infra.build_engine)
    except container_engine.EngineUnavailable as e:
        sys.stderr.write(f"container engine: {e}\n")
        ok = False
    else:
        if engine is not None:
            sys.stderr.write(f"container engine: {engine} (agent overlays build on the host)\n")
        else:
            sys.stderr.write(
                "container engine: none (optional; agent overlays build in the infra "
                "VM — install Docker or Podman to build them on the host)\n"
            )
    missing = pool.missing_taps()
    total = pool.pool_size
    if missing:
        sys.stderr.write(f"TAP pool: {total - len(missing)}/{total} present "
                         f"(missing: {', '.join(missing)})\n")
        ok = False
    else:
        sys.stderr.write(f"TAP pool: {total}/{total} present\n")
    if shutil.which("nft") is None:
        sys.stderr.write(f"nft table inet {pool.nft_table}: unverified "
                         f"(nft not on PATH; enforced + checked post-boot)\n")
    elif pool.nft_table_present():
        sys.stderr.write(f"nft table inet {pool.nft_table}: present\n")
    else:
        sys.stderr.write(f"nft table inet {pool.nft_table}: not confirmable "
                         f"unprivileged (listing needs root; verified post-boot)\n")
    conflicts = pool.overlapping_routes()
    if conflicts:
        detail = ", ".join(f"{c.dst} dev {c.dev}" for c in conflicts)
        sys.stderr.write(f"range overlap: base {pool.ip_base} CLASHES "
                         f"with {detail}\n")
        ok = False
    else:
        sys.stderr.write(f"range overlap: none (base {pool.ip_base})\n")
    _report_persistence()
    if not ok:
        sys.stderr.write("\nRun: bot-bottle install --backend=firecracker\n")
    return 0 if ok else 1


def _report_persistence() -> None:
    """Report whether the pool is a persistent systemd unit. Advisory."""
    if not _has_systemd():
        return
    state = subprocess.run(
        ["systemctl", "is-active", netpool.SYSTEMD_UNIT],
        capture_output=True, text=True, check=False,
    ).stdout.strip() or "unknown"
    if state == "active":
        sys.stderr.write(f"persistence: {netpool.SYSTEMD_UNIT} active "
                         f"(survives reboot)\n")
    else:
        sys.stderr.write(f"persistence: {netpool.SYSTEMD_UNIT} {state} — pool "
                         f"is not installed as a persistent unit (install with "
                         f"`install` so it survives reboot)\n")
