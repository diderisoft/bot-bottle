"""Firecracker host primitives that need no root: locating the binary, kernel
and dropbear, the fail-closed preflight, the rootless rootfs pipeline (image
export -> `mke2fs -d`), and per-bottle SSH keys. The privileged network setup
is in `netpool.py`.
"""

from __future__ import annotations

import contextlib
import json
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from ...contract.hostctl import InfraSettings
from ...environment import LIVE, active_name
from ...lib.log import die, info, warn
from ...paths import host_firecracker_cache_dir
from . import netpool


# The VM is reached as root over SSH; commands `runuser` down to node.
GUEST_SSH_USER = "root"

# Must exist and be openable by the user.
_KVM_DEVICE = "/dev/kvm"


def cache_dir() -> Path:
    return host_firecracker_cache_dir()


def env_dir(name: str | None = None) -> Path:
    """Where an environment's VMs live (`run/`, `infra/`, the lifecycle lock):
    the cache root for live, `envs/<name>/` under it otherwise. The kernel,
    dropbear and root filesystems stay shared in the cache root."""
    name = active_name() if name is None else name
    if name == LIVE:
        return cache_dir()
    d = cache_dir() / "envs" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def kernel_path(infra: InfraSettings) -> Path:
    return Path(infra.fc_kernel) if infra.fc_kernel else cache_dir() / "vmlinux"


def dropbear_path(infra: InfraSettings) -> Path:
    """The static dropbear put in every guest rootfs as its SSH server."""
    return Path(infra.fc_dropbear) if infra.fc_dropbear else cache_dir() / "dropbear"


# --- availability + preflight ---

def is_linux() -> bool:
    return platform.system() == "Linux"


def is_host_capable() -> bool:
    """Linux with KVM, whether or not `firecracker` is installed, so such a
    host selects firecracker and gets an install pointer."""
    return is_linux() and os.path.exists(_KVM_DEVICE)


def is_available() -> bool:
    """Cheap probe: firecracker on PATH, Linux, KVM. Doesn't check the kernel
    or pool, so an unset host errors at launch with a pointer."""
    return is_host_capable() and shutil.which("firecracker") is not None


def require_firecracker(infra: InfraSettings) -> None:
    """Fail-closed preflight: a missing isolation table or TAP pool errors
    rather than boot a VM."""
    if not is_linux():
        die("firecracker backend is only supported on Linux (KVM). "
            "On macOS use the macos-container backend.")
    if shutil.which("firecracker") is None:
        info("Firecracker is required but was not found on PATH.")
        info("Install: https://github.com/firecracker-microvm/firecracker/releases")
        die("firecracker not found on PATH")
    _require_kvm()
    if not kernel_path(infra).is_file():
        die(f"guest kernel not found at {kernel_path(infra)}. Set "
            "BOT_BOTTLE_FC_KERNEL or place a vmlinux there.")
    if not dropbear_path(infra).is_file():
        die(f"static dropbear not found at {dropbear_path(infra)}. Set "
            "BOT_BOTTLE_FC_DROPBEAR or cache one there.")
    if shutil.which("mke2fs") is None:
        die("mke2fs (e2fsprogs) not found — required to build guest rootfs")
    _require_network_pool(netpool.from_settings(infra))


def _require_kvm() -> None:
    if not os.path.exists(_KVM_DEVICE):
        die(f"{_KVM_DEVICE} is missing. Enable KVM (load kvm-intel/kvm-amd; "
            "confirm virtualization is on in firmware).")
    if not os.access(_KVM_DEVICE, os.R_OK | os.W_OK):
        die(f"{_KVM_DEVICE} exists but is not accessible. Add your user to "
            "the `kvm` group and re-login.")


def _require_network_pool(pool: netpool.NetPool) -> None:
    """Check the TAP pool (unprivileged). The nft table is fatal here only when
    `nft` can be queried and says it's absent; otherwise the post-boot probe
    decides. Never boot without the TAP pool."""
    conflicts = pool.overlapping_routes()
    if conflicts:
        detail = "; ".join(f"{c.dst} dev {c.dev}" for c in conflicts)
        warn(f"Firecracker pool range ({pool.ip_base}, "
             f"{pool.pool_size} slots) overlaps existing routes: "
             f"{detail}. This can shadow or be shadowed by that route; "
             f"set BOT_BOTTLE_FC_IP_BASE to a free range and re-run "
             f"bot-bottle install --backend=firecracker.")
    missing = pool.missing_taps()
    if missing:
        die(f"network pool incomplete — missing TAP devices: "
            f"{', '.join(missing)}.\n    bot-bottle install --backend=firecracker")
    if shutil.which("nft") is not None and not pool.nft_table_present():
        # nft answered and the table is absent: fail now.
        warn(f"isolation table `inet {pool.nft_table}` not found via nft. "
             "If this is a permissions issue it will be re-checked "
             "empirically after boot; otherwise run: "
             "bot-bottle install --backend=firecracker")


# --- rootfs pipeline (rootless) ---

def docker_image_id(ref: str) -> str:
    """The image's content digest, the rootfs cache key."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        die(f"docker image inspect for {ref!r} failed: "
            f"{(result.stderr or '').strip() or '<no output>'}")
    return result.stdout.strip().replace("sha256:", "")[:16]


def build_base_rootfs_dir(
    image_ref: str, infra: InfraSettings, *, variant: str = "", init_script: str | None = None,
) -> Path:
    """Export the image's filesystem and inject the guest init and dropbear.
    Cached by image digest; per-bottle bits arrive on the kernel cmdline.
    `variant` keys a different `init_script` for the same image. Returns the
    `mke2fs -d` source dir."""
    digest = docker_image_id(image_ref)
    base = cache_dir() / "rootfs" / f"{digest}{variant}"
    ready = base / ".bb-ready"
    if ready.is_file():
        # A hit refreshes the marker, so its age is time since last use.
        with contextlib.suppress(OSError):
            ready.touch()
        return base

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)

    info(f"exporting {image_ref} rootfs -> {base}")
    export_image_rootfs("docker", image_ref, base)
    inject_guest_boot(base, infra, init_script=init_script)
    ready.write_text("ok\n")
    return base


def export_image_rootfs(engine: str, image_ref: str, dest: Path) -> None:
    """Unpack `image_ref` into `dest` with docker or podman (create, export,
    rm)."""
    cid = subprocess.run(
        [engine, "create", image_ref, "sleep", "infinity"],
        capture_output=True, text=True, check=False,
    )
    if cid.returncode != 0 or not cid.stdout.strip():
        die(f"{engine} create {image_ref!r} failed: {cid.stderr.strip()}")
    container = cid.stdout.strip().splitlines()[-1]
    try:
        export = subprocess.Popen([engine, "export", container], stdout=subprocess.PIPE)
        untar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=export.stdout, check=False)
        export.wait()
        if export.returncode != 0 or untar.returncode != 0:
            die(f"exporting rootfs for {image_ref!r} with {engine} failed")
    finally:
        subprocess.run(
            [engine, "rm", "-f", container],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )


def build_committed_rootfs_dir(tar_path: Path, infra: InfraSettings) -> Path:
    """A base rootfs dir from a frozen-bottle tar: extract, recreate the
    excluded mount points and inject the boot bits. Cached by the tar's size
    and mtime. Returns the `mke2fs -d` source dir."""
    st = tar_path.stat()
    fingerprint = hashlib.sha256(
        f"{tar_path}:{st.st_size}:{st.st_mtime_ns}".encode()
    ).hexdigest()[:16]
    base = cache_dir() / "rootfs" / f"committed-{fingerprint}"
    ready = base / ".bb-ready"
    if ready.is_file():
        return base

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)

    info(f"extracting committed rootfs {tar_path} -> {base}")
    result = subprocess.run(
        ["tar", "-x", "-f", str(tar_path), "-C", str(base)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"extracting committed rootfs {tar_path} failed: "
            f"{result.stderr.strip() or '<no stderr>'}")

    # Recreate the excluded mount points empty for the init and dropbear.
    for mount_point in ("proc", "sys", "dev", "run"):
        (base / mount_point).mkdir(mode=0o755, exist_ok=True)

    inject_guest_boot(base, infra)
    ready.write_text("ok\n")
    return base


def inject_guest_boot(
    rootfs: Path, infra: InfraSettings, init_script: str | None = None,
) -> None:
    """Write the static dropbear and the PID-1 init (`init_script`, default the
    agent init) into the rootfs. A snapshot is guest-controlled, so existing
    entries are replaced with O_EXCL|O_NOFOLLOW files, never followed."""
    _write_staged_file(rootfs / "bb-dropbear", dropbear_path(infra).read_bytes())
    _write_staged_file(rootfs / "bb-init", (init_script or _GUEST_INIT).encode())


def _write_staged_file(path: Path, data: bytes) -> None:
    """Write `data` to `path` (0755) as a fresh regular file, never following a
    symlink. Fails closed on anything unexpected."""
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o755)


def build_rootfs_ext4(base_dir: Path, out_path: Path, *, slack_mib: int = 1024) -> None:
    """A fresh writable ext4 for one bottle from the cached base dir, via
    `mke2fs -d` (no mount). The base stays untouched."""
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


# A bottled agent's home (`/dev/vdb`, PRD 0110): sparse, so it takes only what
# the agent writes. The guest init seeds it from the image's home on first boot.
HOME_IMAGE_SIZE = "16G"


def home_image_path(slug: str) -> Path:
    """Where a bottled agent's home disk lives: its state dir, so it's kept
    and removed with its state."""
    # pylint: disable-next=import-outside-toplevel
    from ...bottle_state import bottle_state_dir
    return bottle_state_dir(slug) / "home.ext4"


def ensure_home_image(slug: str) -> tuple[Path, bool]:
    """The bottled agent's home disk, made empty on its first launch, and
    whether it was already there from an earlier run."""
    path = home_image_path(slug)
    if path.exists():
        info(f"using {slug}'s home from its last run ({path})")
        return path, True
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", str(path), HOME_IMAGE_SIZE],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        die(f"creating {slug}'s home disk failed: {proc.stderr.strip()}")
    return path, False


# A guest kept across stop and resume (`preserve_container`) is grown to this,
# sparse, so an agent with root has room to install things.
GUEST_IMAGE_SIZE = "16G"


def guest_image_path(slug: str) -> Path:
    """A kept guest's root disk (`preserve_container`): in the bottled agent's
    state dir, kept and removed with its state."""
    # pylint: disable-next=import-outside-toplevel
    from ...bottle_state import bottle_state_dir
    return bottle_state_dir(slug) / "guest.ext4"


def kept_guest_image(slug: str, agent_base: Path, infra: InfraSettings, *,
                     fresh: bool) -> tuple[Path, bool]:
    """The kept guest's root disk, made from `agent_base` the first time (or
    when `fresh`), and whether it's the one kept from the last run. A kept one
    gets this host's `/bb-init` and dropbear again: an agent with root could
    have changed them."""
    path = guest_image_path(slug)
    base_note = path.with_name("guest.base")
    if path.exists() and not fresh:
        if base_note.exists() and base_note.read_text().strip() != str(agent_base):
            info(f"{slug} keeps its guest, on its older image; `bot-bottle resume --fresh "
                 f"{slug}` moves it to the current one (its home is kept, its installs aren't)")
        inject_guest_boot_ext4(path, infra)
        info(f"using {slug}'s guest from its last run ({path})")
        return path, True
    path.parent.mkdir(parents=True, exist_ok=True)
    make_bottle_rootfs(agent_base, path)
    grown = subprocess.run(["truncate", "-s", f">{GUEST_IMAGE_SIZE}", str(path)],
                           capture_output=True, text=True, check=False)
    if grown.returncode == 0:
        grown = subprocess.run(["resize2fs", "-f", str(path)],
                               capture_output=True, text=True, check=False)
    if grown.returncode != 0:
        warn(f"couldn't grow {slug}'s guest disk to {GUEST_IMAGE_SIZE}: "
             f"{grown.stderr.strip()}; it has the usual room")
    base_note.write_text(f"{agent_base}\n")
    return path, False


def make_bottle_rootfs(agent_base: Path, out_path: Path) -> None:
    """The per-bottle disk from `agent_base`: a prepared ext4 is copied
    sparsely; a rootfs dir goes through `mke2fs -d`. The base stays pristine."""
    if agent_base.is_file():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["cp", "--sparse=always", str(agent_base), str(out_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            die(f"copying agent rootfs {agent_base} failed: {result.stderr.strip()}")
        return
    build_rootfs_ext4(agent_base, out_path)


def inject_guest_boot_ext4(
    image: Path, infra: InfraSettings, init_script: str | None = None,
) -> None:
    """`inject_guest_boot` for an ext4 image: replace `/bb-dropbear` and
    `/bb-init` with this host's copies (0755, root) via `debugfs -w`."""
    with tempfile.TemporaryDirectory(prefix="bb-guest-boot-") as tmp:
        init = Path(tmp) / "bb-init"
        init.write_bytes((init_script or _GUEST_INIT).encode())
        commands = "\n".join([
            "rm /bb-dropbear",
            "rm /bb-init",
            f"write {dropbear_path(infra)} /bb-dropbear",
            f"write {init} /bb-init",
            *(f"sif /{name} {field} {value}"
              for name in ("bb-dropbear", "bb-init")
              for field, value in (("mode", "0100755"), ("uid", "0"), ("gid", "0"))),
        ]) + "\n"
        script = Path(tmp) / "commands"
        script.write_text(commands)
        # debugfs carries on after a missing-file `rm`; success is checked by
        # reading back.
        subprocess.run(["debugfs", "-w", "-f", str(script), str(image)],
                       capture_output=True, text=True, check=False)
        for name in ("bb-dropbear", "bb-init"):
            stat = subprocess.run(["debugfs", "-R", f"stat /{name}", str(image)],
                                  capture_output=True, text=True, check=False)
            if "Mode:  0755" not in stat.stdout and "Mode:  0100755" not in stat.stdout:
                die(f"writing /{name} into {image} failed: {stat.stderr.strip() or stat.stdout[:200]}")


def grow_ext4(image: Path, slack_mib: int) -> None:
    """Add `slack_mib` free space to an ext4 image (`e2fsck -f`, `resize2fs`)."""
    check = subprocess.run(["e2fsck", "-f", "-y", str(image)],
                           capture_output=True, text=True, check=False)
    if check.returncode not in (0, 1):  # 1 = errors corrected
        die(f"e2fsck {image} failed (exit {check.returncode}): {check.stdout[-300:]}")
    size_mib = (image.stat().st_size >> 20) + slack_mib
    result = subprocess.run(["resize2fs", str(image), f"{size_mib}M"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        die(f"resize2fs {image} failed: {result.stderr.strip()}")


def _dir_size_mib(path: Path) -> int:
    result = subprocess.run(
        ["du", "-sm", str(path)], capture_output=True, text=True, check=False,
    )
    try:
        return int(result.stdout.split()[0])
    except (ValueError, IndexError):
        return 2048


# --- per-bottle SSH keys ---

# Where launch writes the bottle's ENV_VAR_SECRET in the VM.
ENV_VAR_SECRET_PATH = "/run/bot-bottle/env-var-secret"


def guest_ip_from_config(config_path: Path) -> str:
    """The VM's guest IP from its config's `ip=` argument, or ""."""
    try:
        cfg = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    boot_args = cfg.get("boot-source", {}).get("boot_args", "")
    for token in boot_args.split():
        if token.startswith("ip="):
            # ip=<client>::<gw>:<mask>::<dev>:off
            return token[len("ip="):].split(":", 1)[0]
    return ""


def generate_keypair(dest_dir: Path) -> tuple[Path, str]:
    """Mint a per-bottle ed25519 keypair. Returns (private key path, public key
    line); the public line goes on the boot cmdline."""
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
    """SSH options for host->guest control. The VM is ephemeral, so host keys
    aren't checked."""
    return [
        "ssh",
        "-i", str(private_key),
        # Offer only the per-bottle key.
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=5",
        f"{GUEST_SSH_USER}@{guest_ip}",
    ]


# PID-1 init for every guest: POSIX shell and coreutils only. The kernel
# configures eth0; the SSH pubkey arrives base64 on the cmdline; dropbear makes
# ephemeral host keys (-R).
_GUEST_INIT = r"""#!/bin/sh
# bot-bottle Firecracker guest init (PID 1).
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null
mount -o remount,rw / 2>/dev/null

# /tmp must be world-writable + sticky. The rootless rootfs build can land
# it 0755/root-owned, leaving the agent (uid 1000 node) unable to create
# scratch dirs there — git worktrees, build temp, `git init /tmp/...`, etc.
mkdir -p /tmp && chmod 1777 /tmp

# Rootfs export also maps the image's original owners to the unprivileged
# host build uid. That uid is not guaranteed to be node's uid in the guest;
# restore the home-directory boundary before any SSH provisioning runs.
chown node:node /home/node 2>/dev/null || true
chmod 755 /home/node 2>/dev/null || true

# A bottled agent's home disk (/dev/vdb), kept across stop and resume: seeded
# from the image's home on its first boot, then mounted over it.
if [ -b /dev/vdb ]; then
    mkdir -p /mnt/bb-home
    if mount /dev/vdb /mnt/bb-home; then
        if [ ! -e /mnt/bb-home/.bot-bottle-home ]; then
            cp -a /home/node/. /mnt/bb-home/ && rm -rf /mnt/bb-home/lost+found \
                && touch /mnt/bb-home/.bot-bottle-home
        fi
        umount /mnt/bb-home && mount /dev/vdb /home/node
    fi
    chown node:node /home/node 2>/dev/null || true
    chmod 755 /home/node 2>/dev/null || true
fi

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
# Keep restart-recovery key material memory-backed, separate from both the
# agent rootfs and the infra VM's persistent registry volume.
mount -t tmpfs -o mode=0755 tmpfs /run 2>/dev/null || true
# -R: generate host keys on demand. -E: log auth failures to stderr,
# captured in the host-side console.log for debugging. -F: stay in the
# foreground, so dropbear remains this shell's job; without it dropbear
# daemonizes, the shell has no job left, and the reap loop below spins a vCPU.
/bb-dropbear -F -R -E -p 22 &

# Reap zombies as PID 1: `wait` blocks while dropbear runs (and reaps
# orphans meanwhile). The sleep keeps the loop from spinning if it ever exits.
while : ; do wait ; sleep 1 ; done
"""
