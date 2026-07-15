"""Docker-free agent-image builds for the Firecracker backend (PRD 0069 Stage 3).

Instead of `docker build` on the host, an agent's Dockerfile is built inside a
throwaway Firecracker *builder VM* running buildah (rootless, daemonless): no
host Docker daemon, no root-equivalent `docker` group, and the untrusted
Dockerfile executes in a confined microVM rather than on the host.

Flow (on a cache miss, keyed by Dockerfile content):
  1. boot the builder VM (the orchestrator image, which carries buildah) on
     the NAT'd orchestrator link (`netpool.orch_slot()`), so the Dockerfile's
     `FROM` pulls + apt/npm reach the internet;
  2. send the Dockerfile over SSH and `buildah build` it;
  3. `buildah mount` the built image and stream its rootfs tar back to the
     host, extracting it into the cache dir;
  4. inject the guest boot bits + mark the cache ready — the same base-dir
     shape `firecracker.util` turns into a bootable ext4 with `mke2fs -d`.

The builder shares the orchestrator's TAP; once the orchestrator is a
persistent VM (Stage B) these builds move onto its control plane instead of a
throwaway boot.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from ...log import die, info
from . import firecracker_vm, netpool, util

# The builder runs the orchestrator image — it is the component that carries
# buildah (see Dockerfile.orchestrator). Built from the host docker image via
# the bootstrap export path until we pull a pre-built image instead.
_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:latest"

# The builder VM makes DIRECT registry/apt connections (not through the
# gateway proxy the way agents do), and the kernel `ip=` cmdline sets no
# resolver — so give it one. Public for now; build-time egress through the
# gateway will remove the need for it.
_BUILDER_RESOLVER = "1.1.1.1"

# vfs + chroot: buildah works as root in the bare microVM (no fuse-overlayfs /
# overlay module / subuid maps). Must match Dockerfile.orchestrator's env.
# `--isolation` is a build/run-only flag; `from`/`mount` take just the store.
_BUILD_FLAGS = "--isolation chroot --storage-driver vfs"
_STORE_FLAG = "--storage-driver vfs"

_SSH_READY_TIMEOUT = 40.0
_BUILD_TIMEOUT_SECONDS = 900.0
_LOCAL_TAG = "bot-bottle-agent-build"


def _dockerfile_hash(dockerfile: Path) -> str:
    """Cache key: the Dockerfile's content. The shipped agent Dockerfiles
    COPY nothing from the build context (see .dockerignore), so their content
    fully determines the image; a Dockerfile that adds COPY will want the
    context folded in here too."""
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]


def build_agent_rootfs_dir(
    dockerfile: Path, *, image_tag: str, smoke_test: tuple[str, ...] = (),
) -> Path:
    """Build `dockerfile` in a Firecracker builder VM (buildah, no host
    docker), export its rootfs, inject the guest boot bits, and return the
    cached base dir — the same shape `util.build_rootfs_ext4` consumes.
    Cached by Dockerfile content, so a repeat launch skips the rebuild.

    `smoke_test` (the provider's declared argv, e.g. `("claude","--version")`)
    is run in the freshly built image before export, catching an npm
    silent-failure image at build time rather than at first agent use."""
    digest = _dockerfile_hash(dockerfile)
    base = util.cache_dir() / "rootfs" / f"agent-{digest}"
    ready = base / ".bb-ready"
    if ready.is_file():
        info(f"using cached agent rootfs {base.name}")
        return base

    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)

    info(f"building agent image {image_tag!r} in a firecracker builder VM")
    _build_in_vm(dockerfile, base, smoke_test)
    util.inject_guest_boot(base)
    ready.write_text("ok\n")
    return base


def _build_in_vm(dockerfile: Path, base: Path, smoke_test: tuple[str, ...]) -> None:
    """Boot the builder VM, build the Dockerfile with buildah, and stream the
    resulting rootfs into `base`. Tears the VM down on every exit path."""
    slot = netpool.orch_slot()
    if not netpool.tap_present(slot.iface):
        die(f"orchestrator/builder link {slot.iface} not present.\n"
            f"    ./cli.py backend setup --backend=firecracker")

    # The builder runs the orchestrator rootfs (bootstrap: exported from the
    # host docker image; a pulled image replaces this later).
    orch_base = util.build_base_rootfs_dir(_ORCHESTRATOR_IMAGE)
    run_dir = util.cache_dir() / "run" / "builder"
    run_dir.mkdir(parents=True, exist_ok=True)
    rootfs = run_dir / "rootfs.ext4"
    # vfs copies every layer, so the builder disk needs generous headroom.
    util.build_rootfs_ext4(orch_base, rootfs, slack_mib=8192)
    private_key, pubkey = util.generate_keypair(run_dir)

    vm = firecracker_vm.boot(
        name="bot-bottle-builder", rootfs=rootfs, tap=slot.iface,
        guest_ip=slot.guest_ip, host_ip=slot.host_ip, pubkey=pubkey,
        run_dir=run_dir, mem_mib=4096,
    )
    try:
        firecracker_vm.wait_for_ssh(vm, private_key, timeout=_SSH_READY_TIMEOUT)
        _ssh(private_key, slot.guest_ip,
             f"printf 'nameserver {_BUILDER_RESOLVER}\\n' > /etc/resolv.conf "
             f"&& mkdir -p /build/ctx")
        _send_dockerfile(private_key, slot.guest_ip, dockerfile)
        _buildah_build(private_key, slot.guest_ip)
        _smoke_test(private_key, slot.guest_ip, smoke_test)
        _stream_rootfs(private_key, slot.guest_ip, base)
    finally:
        vm.terminate()


def _ssh(private_key: Path, guest_ip: str, script: str,
         *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [script],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _send_dockerfile(private_key: Path, guest_ip: str, dockerfile: Path) -> None:
    proc = subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + ["cat > /build/Dockerfile"],
        input=dockerfile.read_bytes(), capture_output=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        die(f"sending Dockerfile to the builder VM failed: "
            f"{proc.stderr.decode(errors='replace').strip()}")


def _buildah_build(private_key: Path, guest_ip: str) -> None:
    build = _ssh(
        private_key, guest_ip,
        f"buildah build {_BUILD_FLAGS} -t {_LOCAL_TAG} "
        f"-f /build/Dockerfile /build/ctx",
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if build.returncode != 0:
        tail = "\n".join((build.stdout + build.stderr).strip().splitlines()[-20:])
        die(f"buildah build in the builder VM failed:\n{tail}")


def _smoke_test(private_key: Path, guest_ip: str, argv: tuple[str, ...]) -> None:
    """Run the provider's smoke argv inside the freshly built image
    (`buildah run`, which uses the image's own PATH), failing the build
    loudly if the CLI is a broken stub. No-op without a declared test."""
    if not argv:
        return
    cmd = (
        f"set -e; ctr=$(buildah from {_STORE_FLAG} {_LOCAL_TAG}); "
        f"buildah run {_BUILD_FLAGS} \"$ctr\" -- {' '.join(argv)}; rc=$?; "
        f"buildah rm \"$ctr\" >/dev/null 2>&1 || true; exit $rc"
    )
    result = _ssh(private_key, guest_ip, cmd, timeout=120)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()[-10:]
        die(f"agent image failed its post-build smoke test "
            f"({' '.join(argv)}):\n" + "\n".join(detail))


def _stream_rootfs(private_key: Path, guest_ip: str, base: Path) -> None:
    """`buildah mount` the built image in the VM and pipe its rootfs tar
    straight into `base` on the host (extracted as the non-root host user, so
    uid 0 isn't preserved — the guest init restores /root ownership, same as
    the docker-export path)."""
    export = (
        f"set -e; ctr=$(buildah from {_STORE_FLAG} {_LOCAL_TAG}); "
        f"mnt=$(buildah mount {_STORE_FLAG} \"$ctr\"); "
        f"tar -C \"$mnt\" -cf - ."
    )
    ssh_proc = subprocess.Popen(
        util.ssh_base_argv(private_key, guest_ip) + [export],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert ssh_proc.stdout is not None
    untar = subprocess.run(
        ["tar", "-x", "-C", str(base)], stdin=ssh_proc.stdout, check=False,
    )
    ssh_proc.stdout.close()
    ssh_err = (ssh_proc.stderr.read().decode(errors="replace")
               if ssh_proc.stderr else "")
    rc = ssh_proc.wait()
    if rc != 0 or untar.returncode != 0:
        die(f"exporting the built rootfs from the builder VM failed: "
            f"{ssh_err.strip() or '<no stderr>'}")
