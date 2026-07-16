"""Docker-free agent-image builds for the Firecracker backend (PRD 0069 Stage 3).

Agent Dockerfiles build **inside the persistent per-host infra VM**
(`infra_vm.py`), which carries buildah (rootless, daemonless): no host Docker
daemon, no root-equivalent `docker` group. The build runs over SSH against the
infra VM and its rootfs streams back to the host, where the existing
`mke2fs -d` path (`util.build_rootfs_ext4`) turns it into a bootable ext4.

Building in the infra VM — rather than a throwaway builder VM — means there is
one buildah image (`bot-bottle-infra`) and no contention for the orchestrator
TAP. Tradeoff: an untrusted Dockerfile's `RUN` steps share the VM with the
control plane + gateway (buildah `--isolation chroot` isn't a hard boundary) —
the accepted single-VM blast-radius tradeoff, re-splittable into a disposable
builder (booted from this same image on its own TAP) later.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from ...log import die, info
from . import infra_vm, util

# vfs + chroot: buildah works as root in the microVM (no fuse-overlayfs /
# overlay module / subuid maps). `--isolation` is a build/run-only flag;
# `from`/`mount` take just the store.
_BUILD_FLAGS = "--isolation chroot --storage-driver vfs"
_STORE_FLAG = "--storage-driver vfs"

_BUILD_TIMEOUT_SECONDS = 900.0


def _dockerfile_hash(dockerfile: Path) -> str:
    """Cache key: the Dockerfile's content. The shipped agent Dockerfiles
    COPY nothing from the build context (see .dockerignore), so their content
    fully determines the image; a Dockerfile that adds COPY will want the
    context folded in here too."""
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]


def build_agent_rootfs_dir(
    dockerfile: Path, *, image_tag: str, smoke_test: tuple[str, ...] = (),
) -> Path:
    """Build `dockerfile` in the infra VM (buildah, no host docker), export its
    rootfs, inject the guest boot bits, and return the cached base dir — the
    same shape `util.build_rootfs_ext4` consumes. Cached by Dockerfile content,
    so a repeat launch skips the rebuild.

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

    info(f"building agent image {image_tag!r} in the infra VM")
    _build_in_infra(dockerfile, base, smoke_test, digest)
    util.inject_guest_boot(base)
    ready.write_text("ok\n")
    return base


def _build_in_infra(
    dockerfile: Path, base: Path, smoke_test: tuple[str, ...], digest: str,
) -> None:
    """Ensure the infra VM is up, `buildah build` the Dockerfile in it, smoke
    test the image, and stream its rootfs into `base`. The infra VM persists;
    only the per-build container/image/context are cleaned up."""
    infra = infra_vm.ensure_running()
    key, ip = infra.private_key, infra.guest_ip
    tag = f"bot-bottle-agent-build-{digest}"
    ctx = f"/tmp/agent-build-{digest}"
    try:
        prep = _ssh(key, ip, f"rm -rf {ctx}; mkdir -p {ctx}/ctx")
        if prep.returncode != 0:
            die(f"preparing build dir in the infra VM failed: {prep.stderr.strip()}")
        _send_dockerfile(key, ip, dockerfile, ctx)
        _buildah_build(key, ip, ctx, tag)
        _smoke_test(key, ip, tag, smoke_test)
        _stream_rootfs(key, ip, tag, base)
    finally:
        # Keep the ephemeral infra rootfs from accumulating build state. Builds
        # are serial per launch, so removing all working containers is safe.
        _ssh(key, ip,
             f"buildah rm {_STORE_FLAG} -a >/dev/null 2>&1; "
             f"buildah rmi {_STORE_FLAG} {tag} >/dev/null 2>&1; rm -rf {ctx}",
             timeout=60)


def _ssh(private_key: Path, guest_ip: str, script: str,
         *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [script],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _send_dockerfile(private_key: Path, guest_ip: str, dockerfile: Path, ctx: str) -> None:
    proc = subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [f"cat > {ctx}/Dockerfile"],
        input=dockerfile.read_bytes(), capture_output=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        die(f"sending Dockerfile to the infra VM failed: "
            f"{proc.stderr.decode(errors='replace').strip()}")


def _buildah_build(private_key: Path, guest_ip: str, ctx: str, tag: str) -> None:
    build = _ssh(
        private_key, guest_ip,
        f"buildah build {_BUILD_FLAGS} -t {tag} -f {ctx}/Dockerfile {ctx}/ctx",
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if build.returncode != 0:
        tail = "\n".join((build.stdout + build.stderr).strip().splitlines()[-20:])
        die(f"buildah build in the infra VM failed:\n{tail}")


def _smoke_test(private_key: Path, guest_ip: str, tag: str, argv: tuple[str, ...]) -> None:
    """Run the provider's smoke argv inside the freshly built image
    (`buildah run`, which uses the image's own PATH), failing the build
    loudly if the CLI is a broken stub. No-op without a declared test."""
    if not argv:
        return
    cmd = (
        f"set -e; ctr=$(buildah from {_STORE_FLAG} {tag}); "
        f"buildah run {_BUILD_FLAGS} \"$ctr\" -- {' '.join(argv)}; rc=$?; "
        f"buildah rm \"$ctr\" >/dev/null 2>&1 || true; exit $rc"
    )
    result = _ssh(private_key, guest_ip, cmd, timeout=120)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()[-10:]
        die(f"agent image failed its post-build smoke test "
            f"({' '.join(argv)}):\n" + "\n".join(detail))


def _stream_rootfs(private_key: Path, guest_ip: str, tag: str, base: Path) -> None:
    """`buildah mount` the built image in the infra VM and pipe its rootfs tar
    straight into `base` on the host (extracted as the non-root host user, so
    uid 0 isn't preserved — the guest init restores /root ownership)."""
    export = (
        f"set -e; ctr=$(buildah from {_STORE_FLAG} {tag}); "
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
        die(f"exporting the built rootfs from the infra VM failed: "
            f"{ssh_err.strip() or '<no stderr>'}")
