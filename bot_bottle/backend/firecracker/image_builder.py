"""Docker-free agent-image builds for the Firecracker backend (PRD 0069 Stage 3).

Agent Dockerfiles build **inside the persistent per-host orchestrator VM**
(`infra_vm.py`), which carries buildah (rootless, daemonless): no host Docker
daemon, no root-equivalent `docker` group. The build runs over SSH against the
orchestrator VM and its rootfs streams back to the host, where the existing
`mke2fs -d` path (`util.build_rootfs_ext4`) turns it into a bootable ext4.

Builds run on the control-plane (orchestrator) side — not the data-plane
gateway — per PRD 0070 v1 ("builds stay in the orchestrator"): it owns
launches and already carries buildah. Tradeoff: an untrusted Dockerfile's
`RUN` steps share the VM with the control plane (buildah `--isolation chroot`
isn't a hard boundary) — the accepted blast-radius tradeoff, re-splittable
into a disposable builder (booted from this same image on its own TAP) later.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from ... import resources
from ...log import die, info
from . import util
from .infra import FirecrackerInfraService

# vfs + chroot: buildah works as root in the microVM (no fuse-overlayfs /
# overlay module / subuid maps). `--isolation` is a build/run-only flag;
# `from`/`mount` take just the store.
_BUILD_FLAGS = "--isolation chroot --storage-driver vfs"
_STORE_FLAG = "--storage-driver vfs"

_BUILD_TIMEOUT_SECONDS = 900.0


def _dockerfile_hash(dockerfile: Path) -> str:
    """The Dockerfile's content hash. Agent Dockerfiles mostly COPY nothing
    from the build context, so their text nearly determines the built image;
    any files they *do* COPY are folded into `_rootfs_digest` (so a changed
    input busts the cache) and shipped to the VM-side context by
    `_send_build_context`."""
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]


def _context_copy_sources(dockerfile: Path) -> list[str]:
    """The build-context-relative paths a Dockerfile ``COPY``s in.

    Agent Dockerfiles are meant to COPY nothing from the context (the VM-side
    build ships only the Dockerfile), but one may pin an input by COPYing a
    committed file — a checksum list, an npm lockfile. Return those source
    paths so the builder can both ship them to the VM context and fold them
    into the cache key. ``COPY --from=<stage>`` reads a build stage, not the
    context, so it is excluded; the JSON/exec COPY form is unused by the
    shipped images and is skipped rather than mis-parsed."""
    joined = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    sources: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not re.match(r"(?i)^COPY\s", stripped):
            continue
        tokens = stripped.split()[1:]
        if any(t.startswith("--from=") for t in tokens):
            continue
        args = [t for t in tokens if not t.startswith("--")]
        if len(args) < 2 or args[0].startswith("["):
            continue
        sources.extend(args[:-1])
    return sources


def _context_files(dockerfile: Path) -> list[tuple[str, Path]]:
    """``(context-relative path, host path)`` for every existing file a
    Dockerfile COPYs from the build root — globs expanded, sorted, de-duped.
    Absolute or traversing (`..`) sources are dropped: the shipped context
    only ever mirrors files under the build root."""
    root = resources.build_root()
    resolved: dict[str, Path] = {}
    for src in _context_copy_sources(dockerfile):
        if src.startswith("/") or ".." in Path(src).parts:
            continue
        if any(ch in src for ch in "*?["):
            matches = [p for p in root.glob(src) if p.is_file()]
        else:
            candidate = root / src
            if candidate.is_dir():
                matches = [path for path in candidate.rglob("*") if path.is_file()]
            else:
                matches = [candidate] if candidate.is_file() else []
        for path in matches:
            resolved[str(path.relative_to(root))] = path
    return sorted(resolved.items())


def _rootfs_digest(dockerfile: Path) -> str:
    """Cache key for the built AND boot-injected agent rootfs. Its inputs are
    the Dockerfile (the image), the centralized build args, the guest init
    injected into it (`util._GUEST_INIT`), and the content of any files the
    Dockerfile COPYs from the build context. Folding the init in means a fix to
    it — e.g. making /tmp world-writable — busts the cache instead of silently
    reusing a stale rootfs built with the old init; folding the COPYed context
    files in means a repinned input (e.g. a changed checksum list) rebuilds
    rather than reusing a rootfs baked from the old bytes."""
    h = hashlib.sha256()
    h.update(_dockerfile_hash(dockerfile).encode())
    h.update(b"\0")
    for name, value in resources.image_build_args(dockerfile).items():
        h.update(name.encode())
        h.update(b"=")
        h.update(value.encode())
        h.update(b"\0")
    h.update(util._GUEST_INIT.encode())
    for rel, path in _context_files(dockerfile):
        h.update(b"\0")
        h.update(rel.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def cached_agent_rootfs_dir(dockerfile: Path) -> Path | None:
    """Return the ready cached rootfs for ``dockerfile``, if one exists."""
    base = util.cache_dir() / "rootfs" / f"agent-{_rootfs_digest(dockerfile)}"
    return base if (base / ".bb-ready").is_file() else None


def _image_rootfs_digest(image: str) -> str:
    h = hashlib.sha256()
    h.update(image.encode())
    h.update(b"\0")
    h.update(util._GUEST_INIT.encode())
    return h.hexdigest()[:16]


def cached_agent_image_rootfs_dir(image: str) -> Path | None:
    """Return a ready rootfs exported from an immutable OCI image."""
    base = util.cache_dir() / "rootfs" / f"agent-image-{_image_rootfs_digest(image)}"
    return base if (base / ".bb-ready").is_file() else None


def acquire_agent_image_rootfs_dir(
    image: str, *, smoke_test: tuple[str, ...] = (),
) -> Path:
    """Pull a digest-pinned image in the infra VM and export its rootfs."""
    if "@sha256:" not in image:
        die(f"prebuilt Firecracker agent image is not digest-pinned: {image}")
    digest = _image_rootfs_digest(image)
    base = util.cache_dir() / "rootfs" / f"agent-image-{digest}"
    cached = cached_agent_image_rootfs_dir(image)
    if cached is not None:
        info(f"using cached agent rootfs {cached.name}")
        return cached
    with _build_lock():
        if (base / ".bb-ready").is_file():
            return base
        staging = util.cache_dir() / "rootfs" / f".pulling-{digest}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        _pull_in_infra(image, staging, smoke_test, digest)
        util.inject_guest_boot(staging)
        (staging / ".bb-ready").write_text("ok\n")
        shutil.rmtree(base, ignore_errors=True)
        os.rename(staging, base)
    return base


def build_agent_rootfs_dir(
    dockerfile: Path, *, image_tag: str, smoke_test: tuple[str, ...] = (),
) -> Path:
    """Build `dockerfile` in the infra VM (buildah, no host docker), export its
    rootfs, inject the guest boot bits, and return the cached base dir — the
    same shape `util.build_rootfs_ext4` consumes. Cached by Dockerfile content
    + injected guest init, so a repeat launch skips the rebuild but an init or
    Dockerfile change rebuilds.

    `smoke_test` (the provider's declared argv, e.g. `("claude","--version")`)
    is run in the freshly built image before export, catching an npm
    silent-failure image at build time rather than at first agent use."""
    digest = _rootfs_digest(dockerfile)
    base = util.cache_dir() / "rootfs" / f"agent-{digest}"
    if cached_agent_rootfs_dir(dockerfile) is not None:
        info(f"using cached agent rootfs {base.name}")
        return base

    # Serialize builds: the infra VM's buildah store + this cache dir are
    # shared, so concurrent `start`s must not build into them at once. The
    # lock covers the cache lookup + build + atomic publish; the ready
    # fast-path above takes no lock.
    with _build_lock():
        if (base / ".bb-ready").is_file():  # another build finished while we waited
            info(f"using cached agent rootfs {base.name}")
            return base
        # Build into a temp dir and publish by atomic rename, so a partial
        # build is never visible as `agent-<digest>`.
        staging = util.cache_dir() / "rootfs" / f".building-{digest}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        info(f"building agent image {image_tag!r} in the infra VM")
        _build_in_infra(dockerfile, staging, smoke_test, digest)
        util.inject_guest_boot(staging)
        (staging / ".bb-ready").write_text("ok\n")
        shutil.rmtree(base, ignore_errors=True)
        os.rename(staging, base)
    return base


@contextmanager
def _build_lock() -> Generator[None, None, None]:
    """Host-level exclusive lock serializing agent-image builds (shared infra
    buildah store + cache dir). flock auto-releases on a crash."""
    lock_path = util.cache_dir() / "rootfs" / ".build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _build_in_infra(
    dockerfile: Path, base: Path, smoke_test: tuple[str, ...], digest: str,
) -> None:
    """Ensure the infra VMs are up, `buildah build` the Dockerfile in the
    orchestrator VM (the build-capable control plane), smoke test the image,
    and stream its rootfs into `base`. The infra VMs persist; only the
    per-build container/image/context are cleaned up."""
    service = FirecrackerInfraService()
    service.ensure_running()
    key, ip = service.orchestrator().ssh_target()
    tag = f"bot-bottle-agent-build-{digest}"
    ctx = f"/tmp/agent-build-{digest}"
    smoke_ctr, export_ctr = f"{tag}-smoke", f"{tag}-export"

    def _cleanup() -> None:
        # Remove only THIS build's working containers/image/context — never
        # `buildah rm -a`, which would nuke a concurrent build's container.
        _ssh(key, ip,
             f"buildah rm {smoke_ctr} {export_ctr} >/dev/null 2>&1; "
             f"buildah rmi {_STORE_FLAG} {tag} >/dev/null 2>&1; rm -rf {ctx}",
             timeout=60)

    _cleanup()  # clear leftovers from a crashed prior build of this digest
    try:
        prep = _ssh(key, ip, f"mkdir -p {ctx}/ctx")
        if prep.returncode != 0:
            die(f"preparing build dir in the infra VM failed: {prep.stderr.strip()}")
        _send_dockerfile(key, ip, dockerfile, ctx)
        _send_build_context(key, ip, dockerfile, ctx)
        _buildah_build(
            key,
            ip,
            ctx,
            tag,
            resources.image_build_args(dockerfile),
        )
        _smoke_test(key, ip, tag, smoke_ctr, smoke_test)
        _stream_rootfs(key, ip, tag, export_ctr, base)
    finally:
        _cleanup()


def _pull_in_infra(
    image: str, base: Path, smoke_test: tuple[str, ...], digest: str,
) -> None:
    """Pull and export a published agent image inside the orchestrator VM."""
    service = FirecrackerInfraService()
    service.ensure_running()
    key, ip = service.orchestrator().ssh_target()
    tag = f"bot-bottle-agent-pull-{digest}"
    smoke_ctr, export_ctr = f"{tag}-smoke", f"{tag}-export"
    quoted_image = shlex.quote(image)

    def cleanup() -> None:
        _ssh(
            key,
            ip,
            f"buildah rm {smoke_ctr} {export_ctr} >/dev/null 2>&1; "
            f"buildah rmi {_STORE_FLAG} {tag} >/dev/null 2>&1",
            timeout=60,
        )

    cleanup()
    try:
        result = _ssh(
            key,
            ip,
            f"buildah pull {_STORE_FLAG} {quoted_image} && "
            f"buildah tag {_STORE_FLAG} {quoted_image} {tag}",
            timeout=_BUILD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            die(f"pulling pinned agent image failed: {result.stderr.strip()}")
        _smoke_test(key, ip, tag, smoke_ctr, smoke_test)
        _stream_rootfs(key, ip, tag, export_ctr, base)
    finally:
        cleanup()


def _ssh(private_key: Path, guest_ip: str, script: str,
         *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [script],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _ssh_streamed(private_key: Path, guest_ip: str, script: str,
                  *, timeout: float) -> int:
    """Run an SSH command letting the remote's stdout/stderr flow straight to
    ours (no capture), for long chatty steps where live progress beats a
    silent wait. Returns the exit code."""
    proc = subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [script],
        timeout=timeout, check=False,
    )
    return proc.returncode


def _send_dockerfile(private_key: Path, guest_ip: str, dockerfile: Path, ctx: str) -> None:
    proc = subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [f"cat > {ctx}/Dockerfile"],
        input=dockerfile.read_bytes(), capture_output=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        die(f"sending Dockerfile to the infra VM failed: "
            f"{proc.stderr.decode(errors='replace').strip()}")


def _send_build_context(private_key: Path, guest_ip: str, dockerfile: Path, ctx: str) -> None:
    """Ship the files ``dockerfile`` COPYs from the build root into the infra
    VM's ``{ctx}/ctx``, preserving their build-root-relative paths.

    Usually a no-op — agent Dockerfiles COPY nothing — so `{ctx}/ctx` stays the
    empty context the build otherwise runs against. It exists so a Dockerfile
    that pins an input by COPYing a committed file (a checksum list, an npm
    lockfile) still finds that file in the VM-side context. Streamed as a tar
    so directories and multiple files land in one round trip."""
    files = _context_files(dockerfile)
    if not files:
        return
    root = resources.build_root()
    rels = [rel for rel, _ in files]
    tar = subprocess.Popen(
        ["tar", "-C", str(root), "-cf", "-", "--", *rels],
        stdout=subprocess.PIPE,
    )
    try:
        proc = subprocess.run(
            util.ssh_base_argv(private_key, guest_ip) + [f"tar -C {ctx}/ctx -xf -"],
            stdin=tar.stdout, capture_output=True, timeout=120, check=False,
        )
    finally:
        if tar.stdout is not None:
            tar.stdout.close()
        tar.wait()
    if tar.returncode != 0:
        die(f"packing the agent build context failed (tar exit {tar.returncode})")
    if proc.returncode != 0:
        die("sending the agent build context to the infra VM failed: "
            f"{proc.stderr.decode(errors='replace').strip() or '<no stderr>'}")


def _buildah_build(
    private_key: Path,
    guest_ip: str,
    ctx: str,
    tag: str,
    build_args: dict[str, str],
) -> None:
    # Stream buildah's step-by-step output straight to our stderr (like the
    # docker backend's `docker build`), so a long first build (base pull +
    # apt/npm installs) shows live progress instead of a silent wait. The
    # remote stderr is where buildah writes its `STEP i/n` lines.
    info(f"buildah build {tag} in the infra VM (streaming output)")
    arg_flags = " ".join(
        f"--build-arg {shlex.quote(f'{name}={value}')}"
        for name, value in build_args.items()
    )
    rc = _ssh_streamed(
        private_key, guest_ip,
        f"buildah build {_BUILD_FLAGS} {arg_flags} "
        f"-t {tag} -f {ctx}/Dockerfile {ctx}/ctx",
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if rc != 0:
        die(f"buildah build in the infra VM failed (exit {rc}); "
            "see the build output above.")


def _smoke_test(private_key: Path, guest_ip: str, tag: str, ctr: str,
                argv: tuple[str, ...]) -> None:
    """Run the provider's smoke argv inside the freshly built image
    (`buildah run`, which uses the image's own PATH), failing the build
    loudly if the CLI is a broken stub. No-op without a declared test. Uses a
    named working container (`ctr`) so cleanup is scoped to this build."""
    if not argv:
        return
    cmd = (
        f"set -e; buildah from {_STORE_FLAG} --name {ctr} {tag} >/dev/null; "
        f"buildah run {_BUILD_FLAGS} {ctr} -- {' '.join(argv)}; rc=$?; "
        f"buildah rm {ctr} >/dev/null 2>&1 || true; exit $rc"
    )
    result = _ssh(private_key, guest_ip, cmd, timeout=120)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()[-10:]
        die(f"agent image failed its post-build smoke test "
            f"({' '.join(argv)}):\n" + "\n".join(detail))


def _stream_rootfs(private_key: Path, guest_ip: str, tag: str, ctr: str, base: Path) -> None:
    """`buildah mount` the built image in the infra VM and pipe its rootfs tar
    straight into `base` on the host (extracted as the non-root host user, so
    uid 0 isn't preserved — the guest init restores /root ownership). Uses a
    named working container so cleanup is scoped to this build."""
    export = (
        f"set -e; buildah from {_STORE_FLAG} --name {ctr} {tag} >/dev/null; "
        f"mnt=$(buildah mount {_STORE_FLAG} {ctr}); "
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
