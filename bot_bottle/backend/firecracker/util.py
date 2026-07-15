"""Host-side primitives for the Firecracker backend.

Covers the pieces that don't need root at launch time: locating the
firecracker binary / guest kernel / injected dropbear, the fail-closed
preflight (KVM + kernel + isolation table + TAP pool must all be
present before a VM boots), the rootless rootfs pipeline
(`docker export` -> `mke2fs -d`, no mount), and per-bottle SSH key
generation.

The privileged network setup (TAP pool + nft table) is a one-time
operator step — see `netpool.py`, `scripts/firecracker-netpool.sh`,
and `./cli.py backend setup --backend=firecracker`.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from ...log import die, info, warn
from . import netpool


# Guest agent images are Debian-family with USER node; the VM is
# reached over SSH as root (init drops the pubkey into both root's and
# node's authorized_keys) and commands `runuser` down to node.
GUEST_SSH_USER = "root"

# `/dev/kvm` must exist and be openable by the invoking user.
_KVM_DEVICE = "/dev/kvm"


def cache_dir() -> Path:
    d = Path(
        os.environ.get(
            "BOT_BOTTLE_FC_CACHE",
            str(Path.home() / ".cache" / "bot-bottle" / "firecracker"),
        )
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def kernel_path() -> Path:
    return Path(os.environ.get("BOT_BOTTLE_FC_KERNEL", str(cache_dir() / "vmlinux")))


def dropbear_path() -> Path:
    """The static dropbear injected into every guest rootfs as its SSH
    server. Must be statically linked — the guest has none of the
    host's shared libraries."""
    return Path(
        os.environ.get("BOT_BOTTLE_FC_DROPBEAR", str(cache_dir() / "dropbear"))
    )


# --- availability + preflight ---------------------------------------

def is_linux() -> bool:
    return platform.system() == "Linux"


def is_host_capable() -> bool:
    """Whether this host *could* run firecracker — Linux with KVM —
    regardless of whether the `firecracker` binary is installed. Used
    for default-backend selection so a KVM Linux host that hasn't
    installed firecracker yet still selects it and gets an install
    pointer at launch (see `require_firecracker`), rather than silently
    falling back to docker."""
    return is_linux() and os.path.exists(_KVM_DEVICE)


def is_available() -> bool:
    """Cheap capability probe used by cross-backend enumeration —
    firecracker on PATH, on Linux, with KVM. Does not check the
    (operator-provisioned) kernel / pool, so an available-but-unset
    host still shows up and gets an actionable error at launch."""
    return is_host_capable() and shutil.which("firecracker") is not None


def require_firecracker() -> None:
    """Fail-closed preflight. Every check that gates the security
    boundary (the isolation table, the TAP pool) errors rather than
    booting a VM without it."""
    if not is_linux():
        die("firecracker backend is only supported on Linux (KVM). "
            "On macOS use --backend=macos-container.")
    if shutil.which("firecracker") is None:
        info("Firecracker is required but was not found on PATH.")
        info("Install: https://github.com/firecracker-microvm/firecracker/releases")
        die("firecracker not found on PATH")
    _require_kvm()
    if not kernel_path().is_file():
        die(f"guest kernel not found at {kernel_path()}. Set "
            "BOT_BOTTLE_FC_KERNEL or place a vmlinux there.")
    if not dropbear_path().is_file():
        die(f"static dropbear not found at {dropbear_path()}. Set "
            "BOT_BOTTLE_FC_DROPBEAR or cache one there.")
    if shutil.which("mke2fs") is None:
        die("mke2fs (e2fsprogs) not found — required to build guest rootfs")
    _require_network_pool()


def _require_kvm() -> None:
    if not os.path.exists(_KVM_DEVICE):
        die(f"{_KVM_DEVICE} is missing. Enable KVM (load kvm-intel/kvm-amd; "
            "confirm virtualization is on in firmware).")
    if not os.access(_KVM_DEVICE, os.R_OK | os.W_OK):
        die(f"{_KVM_DEVICE} exists but is not accessible. Add your user to "
            "the `kvm` group and re-login.")


def _require_network_pool() -> None:
    """Fail-closed on the parts we can check unprivileged; defer the
    rest to the post-boot isolation probe.

    The TAP pool is verified here (`ip link show` is unprivileged). The
    nft table can only be *confirmed present* here when `nft` is
    queryable — which usually needs root — so a missing/absent nft is
    not treated as fatal at this stage: the authoritative check is the
    empirical isolation probe run after boot, before the agent starts
    (see isolation_probe.verify_isolation). What we must never do is
    boot without the TAP pool."""
    conflicts = netpool.overlapping_routes()
    if conflicts:
        detail = "; ".join(f"{c.dst} dev {c.dev}" for c in conflicts)
        warn(f"Firecracker pool range ({netpool.ip_base()}, "
             f"{netpool.pool_size()} slots) overlaps existing routes: "
             f"{detail}. This can shadow or be shadowed by that route; "
             f"set BOT_BOTTLE_FC_IP_BASE to a free range and re-run "
             f"./cli.py backend setup --backend=firecracker.")
    missing = netpool.missing_taps()
    if missing:
        die(f"network pool incomplete — missing TAP devices: "
            f"{', '.join(missing)}.\n    ./cli.py backend setup --backend=firecracker")
    if shutil.which("nft") is not None and not netpool.nft_table_present():
        # nft is queryable and says the table is absent — that's a
        # definite, catchable misconfiguration; fail early.
        warn(f"isolation table `inet {netpool.NFT_TABLE}` not found via nft. "
             "If this is a permissions issue it will be re-checked "
             "empirically after boot; otherwise run: "
             "./cli.py backend setup --backend=firecracker")


# --- rootfs pipeline (rootless) -------------------------------------

def docker_image_id(ref: str) -> str:
    """The image's content digest, used as the rootfs cache key."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        die(f"docker image inspect for {ref!r} failed: "
            f"{(result.stderr or '').strip() or '<no output>'}")
    return result.stdout.strip().replace("sha256:", "")[:16]


def build_base_rootfs_dir(image_ref: str) -> Path:
    """Export the agent image's filesystem and inject the guest init +
    static dropbear. Cached by image digest — the per-bottle bits
    (authorized_keys, IP) are passed at boot via the kernel cmdline, so
    this tree carries nothing bottle-specific and is safely shared.

    Returns the prepared directory (read as the `mke2fs -d` source)."""
    digest = docker_image_id(image_ref)
    base = cache_dir() / "rootfs" / digest
    ready = base / ".bb-ready"
    if ready.is_file():
        return base

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)

    info(f"exporting {image_ref} rootfs -> {base}")
    cid = subprocess.run(
        ["docker", "create", image_ref, "sleep", "infinity"],
        capture_output=True, text=True, check=False,
    )
    if cid.returncode != 0 or not cid.stdout.strip():
        die(f"docker create {image_ref!r} failed: {cid.stderr.strip()}")
    container = cid.stdout.strip()
    try:
        export = subprocess.Popen(
            ["docker", "export", container], stdout=subprocess.PIPE,
        )
        untar = subprocess.run(
            ["tar", "-x", "-C", str(base)], stdin=export.stdout, check=False,
        )
        export.wait()
        if export.returncode != 0 or untar.returncode != 0:
            die(f"exporting rootfs for {image_ref!r} failed")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

    inject_guest_boot(base)
    ready.write_text("ok\n")
    return base


def inject_guest_boot(rootfs: Path) -> None:
    """Drop the static dropbear and the PID-1 init into the rootfs."""
    shutil.copy2(dropbear_path(), rootfs / "bb-dropbear")
    os.chmod(rootfs / "bb-dropbear", 0o755)
    init = rootfs / "bb-init"
    init.write_text(_GUEST_INIT)
    os.chmod(init, 0o755)


def build_rootfs_ext4(base_dir: Path, out_path: Path, *, slack_mib: int = 1024) -> None:
    """Build a fresh, writable ext4 for one bottle from the cached base
    dir. Rootless: `mke2fs -d` populates the image from a directory
    without mounting. Each call produces an independent disk, so the
    shared base dir stays untouched and concurrent bottles don't race."""
    used_mib = _dir_size_mib(base_dir)
    size_mib = used_mib + slack_mib
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mke2fs", "-q", "-t", "ext4", "-d", str(base_dir), "-F",
         str(out_path), f"{size_mib}M"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"mke2fs for {out_path} failed: {result.stderr.strip()}")


def _dir_size_mib(path: Path) -> int:
    result = subprocess.run(
        ["du", "-sm", str(path)], capture_output=True, text=True, check=False,
    )
    try:
        return int(result.stdout.split()[0])
    except (ValueError, IndexError):
        return 2048


# --- per-bottle SSH keys --------------------------------------------

def generate_keypair(dest_dir: Path) -> tuple[Path, str]:
    """Mint a per-bottle ed25519 keypair. Returns (private_key_path,
    public_key_line). The public line is injected into the guest via
    the boot cmdline; the private key authenticates host->guest SSH."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    key = dest_dir / "bottle_id_ed25519"
    if key.exists():
        key.unlink()
    (dest_dir / "bottle_id_ed25519.pub").unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key),
         "-C", "bot-bottle-firecracker"],
        check=True,
    )
    pub = (dest_dir / "bottle_id_ed25519.pub").read_text().strip()
    return key, pub


def ssh_base_argv(private_key: Path, guest_ip: str) -> list[str]:
    """Common SSH options for host->guest control. The VM is ephemeral
    and per-bottle, so host-key TOFU is meaningless — pin no known_hosts
    and skip the check rather than accumulate churn."""
    return [
        "ssh",
        "-i", str(private_key),
        # Only offer the per-bottle key — don't let an agent or the
        # operator's ~/.ssh/config inject other identities (newer
        # OpenSSH otherwise may not reliably present the -i key).
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=5",
        f"{GUEST_SSH_USER}@{guest_ip}",
    ]


# PID-1 init injected into every guest. Kept dependency-light: relies
# only on coreutils + a POSIX shell (present in the Debian-family agent
# images). The kernel `ip=` cmdline configures eth0 before init runs,
# so no iproute2 is needed. The per-bottle SSH pubkey arrives base64 on
# the cmdline; dropbear generates ephemeral host keys with -R.
_GUEST_INIT = r"""#!/bin/sh
# bot-bottle Firecracker guest init (PID 1).
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null
mount -o remount,rw / 2>/dev/null

# Install the per-bottle SSH pubkey from the kernel cmdline.
KEY=$(sed -n 's/.*bb_pubkey=\([^ ]*\).*/\1/p' /proc/cmdline | base64 -d 2>/dev/null)
if [ -n "$KEY" ]; then
    for home in /root /home/node; do
        mkdir -p "$home/.ssh"
        printf '%s\n' "$KEY" > "$home/.ssh/authorized_keys"
        chmod 700 "$home/.ssh"
        chmod 600 "$home/.ssh/authorized_keys"
    done
    chown -R node:node /home/node/.ssh 2>/dev/null || true
fi

# The rootless rootfs build (`docker export | tar` as a non-root user)
# can't preserve uid 0, so every path lands owned by the build uid,
# which maps to `node` (uid 1000) in-guest — including /root. dropbear
# (like OpenSSH) refuses root's authorized_keys unless the home dir is
# owned by root, so restore root's ownership of its own home.
chown -R 0:0 /root 2>/dev/null || true

mkdir -p /etc/dropbear /run
# -R: generate host keys on demand. -E: log auth failures to stderr,
# captured in the host-side console.log for debugging.
/bb-dropbear -R -E -p 22 &

# Reap zombies as PID 1. dropbear is always a child, so `wait` blocks
# rather than busy-looping.
while : ; do wait ; done
"""
