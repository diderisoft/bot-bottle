"""The per-host infra VM for the Firecracker backend (PRD 0070 Stage B).

A single persistent microVM that runs the orchestrator **control plane** (and,
in a following step, the gateway **data plane**) — the trusted per-host service
the docker backend runs as containers. It boots on the NAT'd orchestrator link
(`netpool.orch_slot()`): the host CLI reaches its control plane over HTTP at the
guest IP, and agent VMs reach its gateway ports over VM-to-VM routing.

Build-from-source (the default while the design churns): the rootfs is exported
from the locally built orchestrator image, which bakes the stdlib-only
control-plane source. A pull-from-registry mode (Gitea's OCI registry) becomes
the default later.

SSH is left enabled for debugging; the control plane is the load-bearing
surface.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shlex
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from ...log import die, info
from ...paths import ORCHESTRATOR_TOKEN_FILENAME, bot_bottle_root
from .. import util as backend_util
from ..docker import util as docker_mod
from ..docker.gateway_provision import GatewayProvisionError
from . import firecracker_vm, infra_artifact, netpool, util

# Where the infra VM keeps its control-plane signing key (generated on the
# persistent /dev/vdb volume mounted at BOT_BOTTLE_ROOT). The host mirrors it
# back so the CLI signs `cli` tokens the VM verifies (issue #469 review).
_GUEST_SIGNING_KEY_PATH = "/var/lib/bot-bottle/orchestrator-token"

# The single infra-VM image: gateway data plane + baked control-plane source
# (Dockerfile.infra FROM the gateway image). Built from source by default;
# a pull-from-registry mode lands later.
_INFRA_IMAGE = "bot-bottle-infra:latest"
_GATEWAY_IMAGE = "bot-bottle-gateway:latest"
_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:latest"
_REPO_ROOT = Path(__file__).resolve().parents[3]

ORCHESTRATOR_PORT = 8099
# Gateway data-plane ports (agent-facing): egress proxy, supervise MCP,
# git-http. Reached by agent VMs over VM-to-VM routing (added next).
EGRESS_PORT = 9099
SUPERVISE_PORT = 9100
GIT_HTTP_PORT = 9420
# mitmproxy writes its CA here a beat after start; agents install it to trust
# the gateway's TLS interception.
_GATEWAY_CA_PATH = "/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem"

# The infra VM makes direct upstream connections (gateway egress, and buildah
# during builds), and the kernel `ip=` cmdline sets no resolver. Public for
# now; routing DNS through a filtered path is a later refinement.
_INFRA_RESOLVER = "1.1.1.1"

_HEALTH_TIMEOUT_SECONDS = 45.0
_HEALTH_POLL_SECONDS = 0.5
_CA_TIMEOUT_SECONDS = 30.0


@dataclass
class InfraVm:
    """A handle to the per-host infra VM: its guest IP and the stable SSH key
    used to fetch the gateway CA / provision git-gate. `vm` is the live VMM
    handle when this process booted it, and None when adopting a singleton a
    prior launcher started (teardown then goes through the PID file)."""

    guest_ip: str
    private_key: Path
    vm: firecracker_vm.VmHandle | None = None

    @property
    def orchestrator_url(self) -> str:
        return f"http://{self.guest_ip}:{ORCHESTRATOR_PORT}"

    def terminate(self) -> None:
        """Stop the infra VM — via the live handle if we booted it, else the
        PID file (adopting-process case)."""
        if self.vm is not None:
            self.vm.terminate()
        else:
            _kill_pidfile()
        _pid_file().unlink(missing_ok=True)

    def gateway_ca_pem(self, *, timeout: float = _CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) that agents install to trust its
        TLS interception. Generated a moment after boot, so this polls over
        SSH until it appears (mirrors DockerGateway.ca_cert_pem)."""
        def _fetch() -> str | None:
            proc = subprocess.run(
                util.ssh_base_argv(self.private_key, self.guest_ip)
                + [f"cat {_GATEWAY_CA_PATH}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            ok = proc.returncode == 0 and "BEGIN CERTIFICATE" in proc.stdout
            return proc.stdout if ok else None
        try:
            return backend_util.poll_ca_cert(_fetch, timeout=timeout)
        except TimeoutError as exc:
            die(str(exc))


def ensure_built() -> None:
    """Ensure the infra rootfs is available before boot.

    Default (docker-free, PRD 0069 Stage 2): download + verify the prebuilt
    rootfs artifact matching this code version (see `infra_artifact`); the
    launch host needs no Docker. `BOT_BOTTLE_INFRA_BUILD=local` instead builds
    the three fixed images from source with host Docker — the infra image
    `COPY --from`s the orchestrator image and is `FROM` the gateway image, so
    both must exist first — for iterating on the Dockerfiles."""
    if infra_artifact.local_build_requested():
        build_infra_images_with_docker()
        return
    infra_artifact.ensure_artifact_gz(
        infra_artifact.infra_artifact_version(_infra_init()))


def build_infra_images_with_docker() -> None:
    """Build the four fixed images from source with host Docker: orchestrator,
    gateway, the shared infra base (Dockerfile.infra), then the Firecracker
    infra image (Dockerfile.infra.fc: FROM infra + buildah). The launch host
    uses this only in `BOT_BOTTLE_INFRA_BUILD=local` mode; `publish_infra`
    uses it off-host to produce the published artifact."""
    docker_mod.build_image(
        _ORCHESTRATOR_IMAGE, str(_REPO_ROOT), dockerfile="Dockerfile.orchestrator")
    docker_mod.build_image(
        _GATEWAY_IMAGE, str(_REPO_ROOT), dockerfile="Dockerfile.gateway")
    docker_mod.build_image(
        "bot-bottle-infra:latest", str(_REPO_ROOT), dockerfile="Dockerfile.infra")
    docker_mod.build_image(
        _INFRA_IMAGE, str(_REPO_ROOT), dockerfile="Dockerfile.infra.fc")


def build_infra_rootfs_dir() -> Path:
    """The infra VM's base rootfs: the infra image prepared with the
    control-plane + gateway init as PID 1. The init's content is folded into
    the cache key so an init change rebuilds the rootfs (the base image digest
    alone wouldn't catch it)."""
    init = _infra_init()
    tag = hashlib.sha256(init.encode()).hexdigest()[:8]
    return util.build_base_rootfs_dir(
        _INFRA_IMAGE, variant=f"-infra-{tag}", init_script=init,
    )


def ensure_running() -> InfraVm:
    """Idempotent per-host singleton. Adopt the infra VM if its control plane
    is already healthy (a prior launcher booted it — it outlives short-lived
    `start` processes); otherwise clear any stale VM and boot a fresh one.
    Returns a handle usable for CA fetch / git-gate provisioning.

    Concurrency-safe: the cold stop/build/boot path is serialized by a host
    flock, so two simultaneous first launches don't both boot on the same
    rootfs/PID. The healthy fast-path takes no lock."""
    slot = netpool.orch_slot()
    url = f"http://{slot.guest_ip}:{ORCHESTRATOR_PORT}"
    key = _infra_dir() / "id_ed25519"
    want = _expected_version()
    if _adoptable(key, url, want):
        info(f"adopting running infra VM at {url}")
        return _with_signing_key(InfraVm(guest_ip=slot.guest_ip, private_key=key))

    with _singleton_lock():
        # Re-check under the lock: another launcher may have booted it while
        # we waited for the lock (double-checked, so we adopt not re-boot).
        if _adoptable(key, url, want):
            info(f"adopting running infra VM at {url}")
            return _with_signing_key(InfraVm(guest_ip=slot.guest_ip, private_key=key))
        # Clear a stale/hung/OUTDATED VM holding the link before booting fresh.
        stop()
        ensure_built()
        infra = boot()
        wait_for_health(infra)
        _record_booted_version(want)
        return _with_signing_key(infra)


def _with_signing_key(infra: InfraVm) -> InfraVm:
    """Mirror the infra VM's control-plane signing key (generated on its
    persistent volume) into the host's orchestrator-token file, so the host CLI
    signs `cli` tokens the VM verifies (issue #469 review). Best-effort: an
    unreadable key is logged, not fatal — the VM still enforces auth, but the CLI
    may then be rejected until the key is readable. Returns `infra` for chaining."""
    proc = subprocess.run(
        util.ssh_base_argv(infra.private_key, infra.guest_ip)
        + [f"cat {_GUEST_SIGNING_KEY_PATH}"],
        capture_output=True, text=True, check=False,
    )
    signing_key = proc.stdout.strip()
    if proc.returncode != 0 or not signing_key:
        info("infra signing key not yet readable; control-plane auth may fail")
        return infra
    path = bot_bottle_root() / ORCHESTRATOR_TOKEN_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(signing_key)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return infra


@contextmanager
def _singleton_lock() -> Generator[None, None, None]:
    """Host-level exclusive lock serializing the infra VM's cold create path
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
    """Stop the infra VM singleton (idempotent — absent is success). Reaps the
    recorded VMM AND any orphaned firecracker still bound to the infra config —
    the PID file drifts after crashes / out-of-band kills, and a survivor would
    hold the orchestrator TAP so the next boot dies with "tap … Resource busy".
    Drops the version marker so a stopped VM is never treated as adoptable."""
    _kill_pidfile()
    _kill_infra_firecrackers()
    _pid_file().unlink(missing_ok=True)
    _version_file().unlink(missing_ok=True)


def boot() -> InfraVm:
    """Boot the infra VM (detached, so it outlives the launcher) on the
    orchestrator link, recording its PID. Prefer `ensure_running`."""
    slot = netpool.orch_slot()
    if not netpool.tap_present(slot.iface):
        die(f"orchestrator link {slot.iface} not present.\n"
            f"    ./cli.py backend setup --backend=firecracker")

    run_dir = _infra_dir()
    rootfs = run_dir / "rootfs.ext4"
    if infra_artifact.local_build_requested():
        util.build_rootfs_ext4(build_infra_rootfs_dir(), rootfs, slack_mib=8192)
    else:
        # Prebuilt artifact already carries the buildah build slack; expand it
        # to a fresh writable rootfs for this boot.
        infra_artifact.materialize_ext4(
            infra_artifact.infra_artifact_version(_infra_init()), rootfs)
    private_key, pubkey = _stable_keypair()

    info(f"booting infra VM on {slot.iface} (guest {slot.guest_ip})")
    vm = firecracker_vm.boot(
        name="bot-bottle-infra", rootfs=rootfs, tap=slot.iface,
        guest_ip=slot.guest_ip, host_ip=slot.host_ip, pubkey=pubkey,
        run_dir=run_dir, mem_mib=4096, detached=True,
        data_drive=_ensure_registry_volume(),
    )
    _pid_file().write_text(str(vm.process.pid))
    return InfraVm(guest_ip=slot.guest_ip, private_key=private_key, vm=vm)


def _infra_dir() -> Path:
    d = util.cache_dir() / "infra"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file() -> Path:
    return _infra_dir() / "vm.pid"


def _version_file() -> Path:
    """Records the infra-artifact version the *running* VM booted from, so a
    later launcher can tell whether the singleton it found is the current code.
    Without it, a healthy VM built from an older image gets adopted forever and
    the new code never boots — every infra change would need an out-of-band
    kill to dislodge the stale VM (and races whatever launched next)."""
    return _infra_dir() / "booted-version"


def _expected_version() -> str:
    return infra_artifact.infra_artifact_version(_infra_init())


def _adoptable(key: Path, url: str, want: str) -> bool:
    """Adopt a running infra VM only if it booted from the CURRENT version and
    its control plane is healthy. A missing/mismatched marker means a prior
    launcher booted an older infra image — reboot rather than reuse stale code."""
    if not key.exists():
        return False
    try:
        booted = _version_file().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return booted == want and _health_ok(url)


def _record_booted_version(version: str) -> None:
    _version_file().write_text(version + "\n", encoding="utf-8")


# The registry "volume": a host-side ext4 file attached to the infra VM as a
# second virtio-block device (guest /dev/vdb), mounted at the control plane's
# DB dir. It outlives the ephemeral rootfs, so the bottle registry survives an
# infra-VM restart — the firecracker analogue of a docker volume. It is a
# plain ext4 file: `sudo mount -o loop <path>` on the host (with the VM
# stopped) to inspect bot-bottle.db directly.
_REGISTRY_SIZE = "512M"


def registry_volume_path() -> Path:
    return _infra_dir() / "registry.ext4"


def _ensure_registry_volume() -> Path:
    """Create the empty ext4 registry volume on first use; reuse it after."""
    vol = registry_volume_path()
    if vol.exists():
        return vol
    info(f"creating infra registry volume {vol} ({_REGISTRY_SIZE})")
    proc = subprocess.run(
        ["mke2fs", "-q", "-t", "ext4", "-F", str(vol), _REGISTRY_SIZE],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        vol.unlink(missing_ok=True)
        die(f"creating registry volume failed: {proc.stderr.strip()}")
    return vol


def _stable_keypair() -> tuple[Path, str]:
    """The infra VM's SSH keypair — generated once and reused, so any later
    launcher can SSH in (fetch CA / provision) even though a different process
    booted the VM. The pubkey is re-injected on every boot via the cmdline."""
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


def _kill_pidfile() -> None:
    """SIGTERM (then SIGKILL) the recorded infra VMM, if it's still ours.
    Guards against a recycled PID by checking the process is firecracker."""
    try:
        pid = int(_pid_file().read_text().strip())
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
    """SIGKILL any firecracker VMM whose `--config-file` is this host's infra
    config, independent of the PID file — reaps orphans it lost track of so the
    orchestrator TAP is free to rebind. Scoped to the infra config path, so the
    interactive pool's agent/infra VMs (other config paths) are untouched."""
    cfg = str(_infra_dir() / "config.json")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() != "firecracker":
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # process vanished / not ours
        if any(a.decode("utf-8", "replace") == cfg for a in args):
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


class SshGatewayTransport:
    """`GatewayTransport` for the gateway running in the infra VM — the docker
    exec/cp equivalents over SSH (dropbear + the stable infra key)."""

    def __init__(self, private_key: Path, guest_ip: str) -> None:
        self._key = private_key
        self._ip = guest_ip

    def exec(self, argv: list[str]) -> None:
        proc = subprocess.run(
            util.ssh_base_argv(self._key, self._ip) + [shlex.join(argv)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"infra gateway exec {argv!r} failed: {proc.stderr.strip()}")

    def cp_into(self, src: str, dest: str) -> None:
        # Preserve the source mode (docker cp does): the access-hook is staged
        # 0700 and git-http execs it directly — a plain `cat >` would land it
        # 0644 and the exec fails with EACCES; keys stay 0600.
        mode = stat.S_IMODE(os.stat(src).st_mode)
        q = shlex.quote(dest)
        proc = subprocess.run(
            util.ssh_base_argv(self._key, self._ip)
            + [f"cat > {q} && chmod {mode:o} {q}"],
            input=Path(src).read_bytes(), capture_output=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"infra gateway cp {src} -> {dest} failed: "
                f"{proc.stderr.decode(errors='replace').strip()}")


def gateway_transport() -> SshGatewayTransport:
    """git-gate provisioning transport for the gateway in the infra VM, built
    from the stable key + the orchestrator link's guest IP. Needs no live VM
    handle, so teardown can use it too."""
    return SshGatewayTransport(
        _infra_dir() / "id_ed25519", netpool.orch_slot().guest_ip)


def wait_for_health(
    infra: InfraVm, *, timeout: float = _HEALTH_TIMEOUT_SECONDS,
) -> None:
    """Poll the control plane's /health until it answers 200 or the deadline
    passes. Dies (with the console tail) if the VMM exits early."""
    url = f"{infra.orchestrator_url}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if infra.vm is not None and not infra.vm.is_alive():
            die(f"infra VM exited during boot (rc={infra.vm.process.returncode}).\n"
                f"{firecracker_vm._console_tail(infra.vm.console_log)}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    info(f"infra control plane healthy at {infra.orchestrator_url}")
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(_HEALTH_POLL_SECONDS)
    tail = (firecracker_vm._console_tail(infra.vm.console_log)
            if infra.vm is not None else "")
    die(f"infra control plane at {url} did not become healthy within "
        f"{timeout:.0f}s.\n{tail}")


def _infra_init() -> str:
    """PID-1 init for the infra VM: mount the pseudo-filesystems, wire a
    resolver, start dropbear (debug SSH), then launch the control plane and
    the gateway data plane (multi-tenant against the local control plane)."""
    return f"""#!/bin/sh
# bot-bottle Firecracker infra VM init (PID 1).
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null
mount -o remount,rw / 2>/dev/null

# Export a real PATH: a bare-init shell resolves its own execs via a
# built-in default path, but that isn't in the *environment*, so
# gateway_init's subprocess daemons (spawned as `python3 ...`) would
# inherit no PATH and fail to find python3. Export it for all children.
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

# Persistent registry volume (second virtio-block device, /dev/vdb) mounted
# at the control plane's DB dir, so bot-bottle.db survives infra-VM restarts.
mount -t ext4 /dev/vdb /var/lib/bot-bottle 2>/dev/null || true

/bb-dropbear -R -E -p 22 &

# Control plane. Source is baked at /app; the package is stdlib-only.
cd /app
# Control-plane signing key + a pre-minted `gateway` JWT, scoped per-process
# (issue #469 review). The key is generated once on the persistent volume and
# handed ONLY to the orchestrator (to verify tokens); the data-plane daemons get
# the `gateway` JWT they present, never the key. Without this the control plane
# would run OPEN and a compromised egress / supervise / git-http daemon in this
# same VM — reaching the orchestrator over 127.0.0.1, past the nft boundary that
# only fences off the separate agent VM — could drive the operator routes
# (approve its own supervise proposals, rewrite policy, read injected tokens).
CP_KEY=$(BOT_BOTTLE_ROOT=/var/lib/bot-bottle python3 -c 'from bot_bottle.paths import host_orchestrator_token as t; print(t())')
GW_JWT=$(BB_SIGNING_KEY="$CP_KEY" python3 -c 'import os; from bot_bottle.orchestrator_auth import mint, ROLE_GATEWAY; print(mint(ROLE_GATEWAY, os.environ["BB_SIGNING_KEY"]))')

BOT_BOTTLE_ROOT=/var/lib/bot-bottle BOT_BOTTLE_ORCHESTRATOR_TOKEN="$CP_KEY" python3 -m bot_bottle.orchestrator \\
    --host 0.0.0.0 --port {ORCHESTRATOR_PORT} --broker stub &

# Gateway data plane, multi-tenant: each request resolves source-IP ->
# policy against the local control plane. The VM backend reaches git over
# git-http (9420), so the git:// daemon (git-gate, needs a per-bottle
# entrypoint the consolidated model doesn't use) is left out. No
# SUPERVISE_DB_PATH: the data plane reaches the supervise queue over the
# control-plane RPC and never opens bot-bottle.db (PRD 0070 / #469). It presents
# the pre-minted `gateway` JWT; gateway_init keeps the signing key out of the
# data-plane daemons' env.
BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise \\
BOT_BOTTLE_ORCHESTRATOR_URL=http://127.0.0.1:{ORCHESTRATOR_PORT} \\
BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT="$GW_JWT" \\
    python3 -m bot_bottle.gateway.bootstrap &

# Reap as PID 1; children are backgrounded, so `wait` blocks.
while : ; do wait ; done
"""
