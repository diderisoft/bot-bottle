"""The firecracker infra VMs: two persistent microVMs, each booting its own
rootfs with its role init as PID 1.

- Orchestrator VM, on the NAT'd orchestrator link: control plane, sole
  opener of `bot-bottle.db` on its `/dev/vdb` volume, holds the signing key,
  runs buildah for agent builds.
- Gateway VM, on its own NAT'd link: egress, git-http and supervise. Agent
  gateway-port traffic is DNAT'd here, never to the orchestrator. Holds the
  CA and a `gateway` token, and reaches the orchestrator at
  `orch_guest:8099` over the link's one nft forward rule.

SSH stays enabled for provisioning and debugging.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from ... import resources
from ...contract.hostctl import HOST_CONTROL, HOSTCTL_URL_ENV, InfraSettings, Settings
from ...lib.log import die, info
from ...paths import HOST_DB_FILENAME
from ..docker import util as docker_mod
from . import firecracker_vm, infra_rootfs, netpool, util

# The signing key, pushed over SSH after boot from the host key file. On the
# rootfs, not the `/dev/vdb` volume: a copy left there by an earlier boot was
# read before the fresh one arrived, so a changed host key got every call a
# 401. Where earlier versions kept it, the init deletes it.
_GUEST_SIGNING_KEY_PATH = "/run/bot-bottle-orchestrator-token"
_STALE_SIGNING_KEY_PATH = "/var/lib/bot-bottle/orchestrator-token"
# The gateway's `gateway` token, on the rootfs (the gateway has no volume),
# pushed after boot.
_GUEST_GATEWAY_JWT_PATH = "/var/lib/bot-bottle/gateway-jwt"
# The orchestrator's host-controller URL and `orchestrator` token, one per line
# (empty when no host controller started it). On the rootfs so a stale copy is
# never read; the init starts the control plane once it arrives.
GUEST_HOSTCTL_PATH = "/run/bot-bottle-hostctl"

# Per-plane rootfs source images: the orchestrator boots
# `Dockerfile.orchestrator.fc` (orchestrator + buildah), the gateway its image
# as-is.
_GATEWAY_IMAGE = "bot-bottle-gateway:latest"
_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:latest"
_ORCHESTRATOR_FC_IMAGE = "bot-bottle-orchestrator-fc:latest"

# Per-role rootfs image and the free space `mke2fs` leaves; the orchestrator
# needs build slack.
_ROOTFS_IMAGE = {"orchestrator": _ORCHESTRATOR_FC_IMAGE, "gateway": _GATEWAY_IMAGE}
_ROOTFS_SLACK_MIB = {"orchestrator": 8192, "gateway": 1024}

ORCHESTRATOR_PORT = 8099

# The orchestrator's persistent buildah store volume (`/dev/vdc`).
GUEST_BUILD_STORE = "/var/lib/containers/overlay-storage"

# The kernel `ip=` arg sets no resolver; the infra VMs connect upstream
# directly.
_INFRA_RESOLVER = "1.1.1.1"

# Retry window for pushing a secret while SSH comes up, shorter than the init's
# own wait.
_SECRET_PUSH_TIMEOUT_SECONDS = 30.0
_SECRET_PUSH_POLL_SECONDS = 0.5


@dataclass
class InfraVm:
    """One infra VM: its guest IP and the stable SSH key. `vm` is the live
    handle when this process booted it, else None (teardown uses the PID file)."""

    guest_ip: str
    private_key: Path
    vm: firecracker_vm.VmHandle | None = None


def role_init(role: str) -> str:
    """The guest PID-1 init for `role`."""
    return _orchestrator_init() if role == "orchestrator" else _gateway_init()


def _role_version(role: str, infra: InfraSettings) -> str:
    return infra_rootfs.role_version(role, infra)


def ensure_built(settings: Settings) -> None:
    """Make both infra rootfs artifacts available before boot: download and
    verify the published ones, or build from source with host Docker when
    `BOT_BOTTLE_INFRA_BUILD=local` or nothing published matches this code."""
    infra = settings.infra
    if infra_rootfs.builds_locally(infra):
        if not infra.infra_build_local and shutil.which("docker") is None:
            die("no published infra image matches this install's code (installed "
                "from main after the last release?) and Docker isn't available to "
                "build one. Install a release, or install Docker.")
        build_infra_images_with_docker(no_cache=settings.operation.no_cache)
        return
    infra_rootfs.ensure_available(infra)


def build_infra_images_with_docker(*, no_cache: bool = False) -> None:
    """Build orchestrator, gateway, then orchestrator-fc (`FROM` orchestrator +
    buildah) with host Docker. Used for local builds and by `publish_infra`."""
    root = str(resources.build_root())
    docker_mod.build_image(
        _ORCHESTRATOR_IMAGE, root, dockerfile="Dockerfile.orchestrator", no_cache=no_cache)
    orchestrator_base = docker_mod.pinned_local_image_ref(_ORCHESTRATOR_IMAGE)
    docker_mod.build_image(
        _GATEWAY_IMAGE, root, dockerfile="Dockerfile.gateway", no_cache=no_cache)
    docker_mod.build_image(
        _ORCHESTRATOR_FC_IMAGE,
        root,
        dockerfile="Dockerfile.orchestrator.fc",
        build_args={"ORCHESTRATOR_BASE_IMAGE": orchestrator_base},
        no_cache=no_cache,
    )


def build_rootfs_dir(role: str, infra: InfraSettings) -> Path:
    """`role`'s base rootfs dir: its image with the role init as PID 1. The
    init's content is part of the cache key."""
    init = role_init(role)
    tag = hashlib.sha256(init.encode()).hexdigest()[:8]
    return util.build_base_rootfs_dir(
        _ROOTFS_IMAGE[role], infra, variant=f"-{role}-{tag}", init_script=init,
    )


@contextmanager
def singleton_lock() -> Generator[None, None, None]:
    """Host lock over the pair's cold path (`stop`/`ensure_built`/`boot`)."""
    lock_path = _infra_dir() / "singleton.lock"
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def stop() -> None:
    """Stop both infra VMs (idempotent): the recorded VMMs, any firecracker
    still bound to an infra config (PID files drift), and a legacy combined VM.
    Survivors would hold a link's TAP. Drops the version marker."""
    _kill_pidfile(_orch_dir())
    _kill_pidfile(_gw_dir())
    _kill_pidfile(_infra_dir())  # legacy combined VM
    _kill_infra_firecrackers()
    _pid_file(_orch_dir()).unlink(missing_ok=True)
    _pid_file(_gw_dir()).unlink(missing_ok=True)
    _pid_file(_infra_dir()).unlink(missing_ok=True)  # legacy
    _version_file().unlink(missing_ok=True)


def boot_vm(
    *,
    settings: Settings,
    name: str,
    slot: netpool.Slot,
    run_dir: Path,
    role: str,
    mem_mib: int,
    data_drives: Sequence[Path] = (),
    extra_boot_args: str = "",
) -> InfraVm:
    """Boot the `role` VM from its rootfs on `slot`'s link and record the PID."""
    if not netpool.tap_present(slot.iface):
        die(f"infra link {slot.iface} not present.\n"
            f"    bot-bottle backend setup --backend=firecracker")

    run_dir.mkdir(parents=True, exist_ok=True)
    rootfs = run_dir / "rootfs.ext4"
    infra = settings.infra
    if infra_rootfs.builds_locally(infra):
        util.build_rootfs_ext4(
            build_rootfs_dir(role, infra), rootfs, slack_mib=_ROOTFS_SLACK_MIB[role])
    else:
        # Expand the artifact to a fresh writable rootfs for this boot.
        infra_rootfs.materialize_ext4(role, rootfs, infra)
    private_key, pubkey = _stable_keypair()

    info(f"booting {role} VM on {slot.iface} (guest {slot.guest_ip})")
    boot_args = extra_boot_args
    vm = firecracker_vm.boot(
        name=name, kernel=util.kernel_path(infra), rootfs=rootfs, tap=slot.iface,
        guest_ip=slot.guest_ip, host_ip=slot.host_ip, pubkey=pubkey,
        run_dir=run_dir, mem_mib=mem_mib, detached=True,
        data_drives=data_drives, extra_boot_args=boot_args,
    )
    _pid_file(run_dir).write_text(str(vm.process.pid))
    return InfraVm(guest_ip=slot.guest_ip, private_key=private_key, vm=vm)


def _infra_dir() -> Path:
    d = util.cache_dir() / "infra"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _orch_dir() -> Path:
    d = _infra_dir() / "orchestrator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gw_dir() -> Path:
    d = _infra_dir() / "gateway"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file(run_dir: Path) -> Path:
    return run_dir / "vm.pid"


def _version_file() -> Path:
    """Records the version the running pair booted from, so a later launcher
    replaces a pair running older code instead of adopting it."""
    return _infra_dir() / "booted-version"


def expected_version(infra: InfraSettings) -> str:
    """The marker for the pair: both per-plane artifact versions."""
    return " ".join(f"{role}={_role_version(role, infra)}" for role in infra_rootfs.ROLES)


def adoptable(key: Path, url: str, want: str) -> bool:
    """Adopt the running pair only if it booted the current version, the
    orchestrator is healthy and the gateway VM is alive."""
    if not key.exists():
        return False
    try:
        booted = _version_file().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if booted != want:
        return False
    if not _health_ok(url):
        return False
    return _pidfile_alive(_gw_dir())


def record_booted_version(version: str) -> None:
    _version_file().write_text(version + "\n", encoding="utf-8")


def push_secret(infra: InfraVm, secret: str, dest: str, what: str) -> None:
    """Pipe `secret` to an atomic write of `dest` over SSH, retrying while SSH
    comes up; die naming `what` on failure. Off argv."""
    push = f"umask 077; cat > {dest}.tmp && mv {dest}.tmp {dest}"
    deadline = time.monotonic() + _SECRET_PUSH_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            util.ssh_base_argv(infra.private_key, infra.guest_ip) + [push],
            input=secret, capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return
        last = proc.stderr.strip()
        time.sleep(_SECRET_PUSH_POLL_SECONDS)
    die(f"could not push {what}: {last or '<no stderr>'}")


def push_stream(infra: InfraVm, data: bytes, script: str, what: str) -> None:
    """Pipe `data` to `script` over SSH with the same retry, for larger pushes."""
    deadline = time.monotonic() + _SECRET_PUSH_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            util.ssh_base_argv(infra.private_key, infra.guest_ip) + [script],
            input=data, capture_output=True, check=False,
        )
        if proc.returncode == 0:
            return
        last = proc.stderr.decode(errors="replace").strip()
        time.sleep(_SECRET_PUSH_POLL_SECONDS)
    die(f"could not push {what}: {last or '<no stderr>'}")


def gateway_ca_dir() -> Path:
    """The firecracker gateway CA's host dir. Separate from the docker/macOS
    dir, which docker writes as root with a 0600 key."""
    d = _infra_dir() / "gateway-ca"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _stable_keypair() -> tuple[Path, str]:
    """The infra VMs' shared SSH keypair, created once so any launcher can
    reach VMs another booted. Injected on every boot via the cmdline."""
    d = _infra_dir()
    key, pub = d / "id_ed25519", d / "id_ed25519.pub"
    if key.exists() and pub.exists():
        return key, pub.read_text().strip()
    key.unlink(missing_ok=True)
    pub.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key),
         "-C", "bot-bottle-infra"],
        check=True,
    )
    return key, pub.read_text().strip()


def _pidfile_alive(run_dir: Path) -> bool:
    """True iff `run_dir`'s recorded VMM is a live firecracker (checks `comm`)."""
    try:
        pid = int(_pid_file(run_dir).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() == "firecracker"
    except OSError:
        return False


def _kill_pidfile(run_dir: Path) -> None:
    """SIGTERM, then SIGKILL, `run_dir`'s VMM if it's still firecracker."""
    try:
        pid = int(_pid_file(run_dir).read_text().strip())
    except (OSError, ValueError):
        return
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return  # already gone
    if comm != "firecracker":
        return  # PID reused by another process
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists():
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _kill_infra_firecrackers(proc_root: Path = Path("/proc")) -> None:
    """SIGKILL any firecracker whose `--config-file` is one of this host's
    infra configs (including the legacy combined one), regardless of PID files.
    Agent VMs are untouched."""
    cfgs = {
        str(_orch_dir() / "config.json"),
        str(_gw_dir() / "config.json"),
        str(_infra_dir() / "config.json"),  # legacy combined VM
    }
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "firecracker":
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # vanished or not ours
        if any(a.decode("utf-8", "replace") in cfgs for a in args):
            try:
                os.kill(int(entry.name), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=1.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _init_head() -> str:
    """Shared PID-1 preamble: mount pseudo-filesystems, export PATH (children
    need it in the environment), set the resolver, install the cmdline SSH
    pubkey and start dropbear."""
    return f"""#!/bin/sh
# bot-bottle Firecracker infra VM init (PID 1).
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null
mount -o remount,rw / 2>/dev/null

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Direct upstream resolver (control-plane / gateway egress + buildah).
printf 'nameserver {_INFRA_RESOLVER}\\n' > /etc/resolv.conf 2>/dev/null

# Debug SSH: install the per-boot pubkey from the kernel cmdline.
KEY=$(sed -n 's/.*bb_pubkey=\\([^ ]*\\).*/\\1/p' /proc/cmdline | base64 -d 2>/dev/null)
if [ -n "$KEY" ]; then
    mkdir -p /root/.ssh
    printf '%s\\n' "$KEY" > /root/.ssh/authorized_keys
    chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
fi
chown -R 0:0 /root 2>/dev/null || true
mkdir -p /etc/dropbear /run /var/lib/bot-bottle

/bb-dropbear -R -E -p 22 &

cd /app
"""


_INIT_TAIL = """
# Reap as PID 1; children are backgrounded, so `wait` blocks.
while : ; do wait ; done
"""


def _gateway_init() -> str:
    """Gateway VM init: wait for the `gateway` token, then start egress,
    git-http and supervise against the orchestrator at `bb_orch`. No git://
    daemon. Refuses to start without the token."""
    return _init_head() + f"""
ORCH=$(sed -n 's/.*bb_orch=\\([^ ]*\\).*/\\1/p' /proc/cmdline)
GW_JWT=""
i=0
while [ "$i" -lt 600 ]; do
    GW_JWT=$(cat {_GUEST_GATEWAY_JWT_PATH} 2>/dev/null)
    [ -n "$GW_JWT" ] && break
    i=$((i + 1))
    sleep 0.1
done
if [ -z "$GW_JWT" ]; then
    echo "infra gateway: gateway JWT never arrived; refusing to start the data plane" >&2
else
    chmod 600 {_GUEST_GATEWAY_JWT_PATH} 2>/dev/null || true
    BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise \\
    BOT_BOTTLE_ORCHESTRATOR_URL=http://$ORCH:{ORCHESTRATOR_PORT} \\
    BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT="$GW_JWT" \\
        python3 -m bot_bottle.gateway.bootstrap &
fi
""" + _INIT_TAIL


def _orchestrator_init() -> str:
    """Orchestrator VM init: mount the `/dev/vdb` registry volume, wait for the
    signing key, start the control plane. Refuses to start without the key."""
    return _init_head() + f"""
# Persistent registry volume (second virtio-block device, /dev/vdb) mounted at
# the DB dir, so bot-bottle.db survives orchestrator-VM restarts.
mount -t ext4 /dev/vdb /var/lib/bot-bottle 2>/dev/null || true
rm -f {_STALE_SIGNING_KEY_PATH}
# Persistent buildah store (/dev/vdc), so pulled bases and layer cache survive
# restarts. Unmounted, buildah falls back to the ephemeral rootfs.
mkdir -p {GUEST_BUILD_STORE}
mount -t ext4 /dev/vdc {GUEST_BUILD_STORE} 2>/dev/null || true
CP_KEY=""
i=0
while [ "$i" -lt 600 ]; do
    CP_KEY=$(cat {_GUEST_SIGNING_KEY_PATH} 2>/dev/null)
    [ -n "$CP_KEY" ] && break
    i=$((i + 1))
    sleep 0.1
done
i=0
while [ "$i" -lt 600 ] && [ ! -f {GUEST_HOSTCTL_PATH} ]; do
    i=$((i + 1))
    sleep 0.1
done
HC_URL=$(sed -n 1p {GUEST_HOSTCTL_PATH} 2>/dev/null)
HC_JWT=$(sed -n 2p {GUEST_HOSTCTL_PATH} 2>/dev/null)
if [ -z "$CP_KEY" ]; then
    echo "infra: control-plane signing key never arrived; refusing to start the control plane (would run OPEN)" >&2
else
    chmod 600 {_GUEST_SIGNING_KEY_PATH} 2>/dev/null || true
    BOT_BOTTLE_ORCHESTRATOR_TOKEN="$CP_KEY" \\
    {HOSTCTL_URL_ENV}="$HC_URL" {HOST_CONTROL.token_env}="$HC_JWT" \\
        python3 -m bot_bottle.orchestrator \\
        --host 0.0.0.0 --port {ORCHESTRATOR_PORT} \\
        --db /var/lib/bot-bottle/db/{HOST_DB_FILENAME} &
fi
""" + _INIT_TAIL
