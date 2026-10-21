"""The per-host infra VMs for the Firecracker backend (PRD 0070).

Two persistent microVMs, split now that #469 got the DB off the data plane
(PRD 0070 "Separating the planes"):

  * **orchestrator VM** — the control plane. Boots on the NAT'd orchestrator
    link (`netpool.orch_slot()`); the host CLI reaches its `/health` +
    operator routes over HTTP at the guest IP. Sole opener of `bot-bottle.db`
    (on its persistent /dev/vdb registry volume); holds the host-canonical
    signing key (pushed post-boot). Also carries buildah, so in-VM agent-image
    builds run here (PRD 0070 v1: builds stay with the control plane).
  * **gateway VM** — the data plane. Boots on its own NAT'd link
    (`netpool.gw_slot()`); runs the egress / git-http / supervise daemons that
    agent VMs reach (their gateway-port traffic is DNAT'd here — never to the
    orchestrator, so a breached agent has no L3 route to the control plane).
    Holds the mitmproxy CA + a pre-minted `gateway` JWT (never the signing
    key); reaches the orchestrator's control plane at `orch_guest:8099` over
    the one nft forward rule that link allows.

Each VM boots its **own** per-plane rootfs — the orchestrator rootfs carries the
control plane + buildah (in-VM agent builds), the gateway rootfs is the slim
data plane with no build tooling on the exposed VM. Two artifacts, built/pulled
per role (`infra_rootfs`); each rootfs bakes only its own role init as PID 1.
The gateway VM also runs a slimmer memory ceiling.

SSH is left enabled for debugging + provisioning; the control plane is the
load-bearing surface.
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
from ...contract.hostctl import InfraSettings, Settings
from ...lib.log import die, info
from ...paths import HOST_DB_FILENAME
from ..docker import util as docker_mod
from . import firecracker_vm, infra_rootfs, netpool, util

# The orchestrator VM's signing-key path on its persistent /dev/vdb volume
# (mounted at /var/lib/bot-bottle). The launcher seeds this
# file with the host-canonical key AFTER boot (over SSH), so the VM verifies
# tokens with the same key the host CLI signs with — the host token file stays
# the single source of truth, never clobbered per-backend (issue #469 review).
_GUEST_SIGNING_KEY_PATH = "/var/lib/bot-bottle/orchestrator-token"
# The gateway VM's pre-minted `gateway` JWT path (rootfs, not /dev/vdb — the
# data plane has no registry volume and never opens the DB). Pushed post-boot;
# the gateway daemons present it to the orchestrator, and never see the key.
_GUEST_GATEWAY_JWT_PATH = "/var/lib/bot-bottle/gateway-jwt"

# The two per-plane rootfs source images. The orchestrator VM boots a control
# plane + buildah rootfs (Dockerfile.orchestrator.fc, FROM orchestrator); the
# gateway VM boots the slim data-plane image directly (no build tooling on the
# exposed VM). Built from source by default; the launch host pulls prebuilt
# artifacts instead (`infra_rootfs`).
_GATEWAY_IMAGE = "bot-bottle-gateway:latest"
_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:latest"
_ORCHESTRATOR_FC_IMAGE = "bot-bottle-orchestrator-fc:latest"

# Per-role rootfs source image + the extra free space `mke2fs` leaves for the
# guest to grow into. The orchestrator keeps buildah's large build slack; the
# gateway carries no build tooling, so its rootfs is much smaller.
_ROOTFS_IMAGE = {"orchestrator": _ORCHESTRATOR_FC_IMAGE, "gateway": _GATEWAY_IMAGE}
_ROOTFS_SLACK_MIB = {"orchestrator": 8192, "gateway": 1024}

ORCHESTRATOR_PORT = 8099

# Where the orchestrator VM mounts its persistent buildah store volume
# (/dev/vdc); image_builder points buildah's `--root` here.
GUEST_BUILD_STORE = "/var/lib/containers/overlay-storage"

# The infra VMs make direct upstream connections (gateway egress, and buildah
# during builds), and the kernel `ip=` cmdline sets no resolver. Public for
# now; routing DNS through a filtered path is a later refinement.
_INFRA_RESOLVER = "1.1.1.1"

# How long the launcher retries pushing a secret while the guest's SSH comes
# up. Below the init's own wait window, so a failed push dies here first.
_SECRET_PUSH_TIMEOUT_SECONDS = 30.0
_SECRET_PUSH_POLL_SECONDS = 0.5


@dataclass
class InfraVm:
    """A handle to one infra VM: its guest IP and the stable SSH key used to
    seed secrets / fetch the CA / provision git-gate. `vm` is the live VMM
    handle when this process booted it, and None when adopting a singleton a
    prior launcher started (teardown then goes through the PID file)."""

    guest_ip: str
    private_key: Path
    vm: firecracker_vm.VmHandle | None = None


def role_init(role: str) -> str:
    """The guest PID-1 init for `role` (each per-plane rootfs bakes only its
    own — no `bb_role` branch, since the rootfs *is* the role)."""
    return _orchestrator_init() if role == "orchestrator" else _gateway_init()


def _role_version(role: str, infra: InfraSettings) -> str:
    return infra_rootfs.role_version(role, infra)


def ensure_built(settings: Settings) -> None:
    """Ensure both infra rootfs artifacts are available before boot.

    Default (docker-free, PRD 0069 Stage 2): download + verify the prebuilt
    orchestrator + gateway rootfs artifacts matching this code version (see
    `infra_rootfs`); the launch host needs no Docker.
    It builds the images from source with host Docker instead (the
    orchestrator-fc image is `FROM` the orchestrator image, so it must exist
    first) when asked to (`BOT_BOTTLE_INFRA_BUILD=local`) or when no published
    image matches this install's code (`infra_rootfs.builds_locally`)."""
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
    """Build the fixed images from source with host Docker: orchestrator,
    gateway, then the orchestrator-fc image (Dockerfile.orchestrator.fc: FROM
    orchestrator + buildah). The gateway VM boots the gateway image directly.
    The launch host uses this only in `BOT_BOTTLE_INFRA_BUILD=local` mode;
    `publish_infra` uses it off-host to produce the published artifacts."""
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
    """`role`'s base rootfs dir: its source image prepared with the role init as
    PID 1. The init's content is folded into the cache key so an init change
    rebuilds the rootfs (the base image digest alone wouldn't catch it)."""
    init = role_init(role)
    tag = hashlib.sha256(init.encode()).hexdigest()[:8]
    return util.build_base_rootfs_dir(
        _ROOTFS_IMAGE[role], infra, variant=f"-{role}-{tag}", init_script=init,
    )


@contextmanager
def singleton_lock() -> Generator[None, None, None]:
    """Host-level exclusive lock serializing the infra pair's cold create path
    (`stop`/`ensure_built`/`boot`). flock auto-releases if the launcher
    crashes, so the lock is never leaked."""
    lock_path = _infra_dir() / "singleton.lock"
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def stop() -> None:
    """Stop BOTH infra VMs (idempotent — absent is success). Reaps the
    recorded VMMs AND any orphaned firecracker still bound to either infra
    config — the PID files drift after crashes / out-of-band kills, and a
    survivor would hold a link's TAP so the next boot dies with "tap …
    Resource busy". Also reaps a surviving *legacy* single combined-VM (the
    pre-split layout booted from `<infra>/config.json` on the orchestrator
    link): the two-VM `stop` otherwise wouldn't know about it, and it would
    hold the orchestrator TAP so the first split boot fails — so the cutover
    is self-healing, no manual host teardown. Drops the version marker so a
    stopped pair is never treated as adoptable."""
    _kill_pidfile(_orch_dir())
    _kill_pidfile(_gw_dir())
    _kill_pidfile(_infra_dir())  # legacy pre-split combined VM (migration)
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
    """Boot the `role` infra VM from its per-plane rootfs on `slot`'s link.
    Records the PID."""
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
        # Prebuilt artifact already carries the role's build slack; expand it to
        # a fresh writable rootfs for this boot.
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
    """Records the infra-artifact version the *running* pair booted from, so a
    later launcher can tell whether the singletons it found are the current
    code. Without it, a healthy pair built from an older image gets adopted
    forever and the new code never boots — every infra change would need an
    out-of-band kill to dislodge the stale VMs (and races whatever launched
    next). Both VMs boot the same artifact, so one marker covers the pair."""
    return _infra_dir() / "booted-version"


def expected_version(infra: InfraSettings) -> str:
    """The combined marker for the running pair: both per-plane artifact
    versions, so a change to either rootfs dislodges the adopted pair."""
    return " ".join(f"{role}={_role_version(role, infra)}" for role in infra_rootfs.ROLES)


def adoptable(key: Path, url: str, want: str) -> bool:
    """Adopt the running pair only if it booted from the CURRENT version, the
    orchestrator's control plane is healthy, and the gateway VM is still alive.
    A missing/mismatched marker means a prior launcher booted an older infra
    image; a dead gateway means the pair is half-down — reboot both rather than
    reuse stale or partial state."""
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
    """Pipe `secret` to an atomic write of `dest` in the guest over SSH,
    retrying while the guest's SSH comes up; die (naming `what`) if it never
    succeeds. Bare-pipe input keeps the value off argv."""
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
    """Pipe `data` to `script` in the guest over SSH, retrying while the
    guest's SSH comes up; die (naming `what`) if it never succeeds. For file
    pushes larger than a single secret (a tar of CA material)."""
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
    """Where the firecracker gateway's mitmproxy CA persists on the host, so a
    gateway reboot reuses it rather than minting a new one. Its own dir, not
    the docker/macOS bind-mount dir: the docker gateway writes that one as
    root, with the private key mode 0600, so a non-root launcher couldn't read
    it."""
    d = _infra_dir() / "gateway-ca"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _stable_keypair() -> tuple[Path, str]:
    """The infra VMs' shared SSH keypair — generated once and reused, so any
    later launcher can SSH in (seed secrets / fetch CA / provision) even though
    a different process booted the VMs. Both VMs get the same pubkey re-injected
    on every boot via the cmdline."""
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
    """True iff `run_dir`'s recorded VMM is still a live firecracker (guards a
    recycled PID by checking `comm`)."""
    try:
        pid = int(_pid_file(run_dir).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() == "firecracker"
    except OSError:
        return False


def _kill_pidfile(run_dir: Path) -> None:
    """SIGTERM (then SIGKILL) the VMM recorded in `run_dir`, if it's still ours.
    Guards against a recycled PID by checking the process is firecracker."""
    try:
        pid = int(_pid_file(run_dir).read_text().strip())
    except (OSError, ValueError):
        return
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return  # already gone
    if comm != "firecracker":
        return  # PID recycled by an unrelated process
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
    """SIGKILL any firecracker VMM whose `--config-file` is one of this host's
    infra configs (orchestrator or gateway), independent of the PID files —
    reaps orphans it lost track of so both links' TAPs are free to rebind.
    Also matches the *legacy* pre-split combined-VM config (`<infra>/config.json`)
    so a surviving old singleton is cleared off the orchestrator link during the
    cutover. Scoped to these infra config paths, so the pool's agent VMs (other
    config paths) are untouched."""
    cfgs = {
        str(_orch_dir() / "config.json"),
        str(_gw_dir() / "config.json"),
        str(_infra_dir() / "config.json"),  # legacy pre-split combined VM
    }
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "firecracker":
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # process vanished / not ours
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
    """The shared PID-1 preamble both role inits open with: mount the pseudo-
    filesystems, export a real PATH (a bare-init shell's built-in exec path
    isn't in the *environment*, so backgrounded `python3 ...` children would
    find no PATH), set the direct upstream resolver, install the per-boot SSH
    pubkey from the cmdline, and start dropbear for debug/provisioning."""
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
    """PID-1 init for the gateway (data-plane) VM. Waits for the host-seeded
    `gateway` JWT, then starts ONLY the data-plane daemons, multi-tenant against
    the orchestrator at the `bb_orch` cmdline address. The VM backend reaches git
    over git-http (9420), so the git:// daemon (a per-bottle entrypoint the
    consolidated model doesn't use) is left out. No SUPERVISE_DB_PATH: the data
    plane reaches the supervise queue over the control-plane RPC and never opens
    bot-bottle.db (PRD 0070 / #469). If the JWT never arrives, REFUSE to start
    rather than run without auth."""
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
    """PID-1 init for the orchestrator (control-plane) VM. Mounts the persistent
    registry volume (/dev/vdb — bot-bottle.db survives a VM restart), waits for
    the host-seeded signing key, then starts ONLY the control plane. If the key
    never arrives, REFUSE to start rather than run OPEN — open mode would grant
    every unauthenticated caller the `cli` role (#469)."""
    return _init_head() + f"""
# Persistent registry volume (second virtio-block device, /dev/vdb) mounted at
# the DB dir, so bot-bottle.db survives orchestrator-VM restarts.
mount -t ext4 /dev/vdb /var/lib/bot-bottle 2>/dev/null || true
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
if [ -z "$CP_KEY" ]; then
    echo "infra: control-plane signing key never arrived; refusing to start the control plane (would run OPEN)" >&2
else
    chmod 600 {_GUEST_SIGNING_KEY_PATH} 2>/dev/null || true
    BOT_BOTTLE_ORCHESTRATOR_TOKEN="$CP_KEY" python3 -m bot_bottle.orchestrator \\
        --host 0.0.0.0 --port {ORCHESTRATOR_PORT} \\
        --db /var/lib/bot-bottle/db/{HOST_DB_FILENAME} &
fi
""" + _INIT_TAIL
