"""Agent bases for the Firecracker backend (PRD 0069 Stage 3, PRD 0085).

Three sources, each cached under the host cache dir: a published ext4 image,
downloaded, verified and prepared on the host; an overlay on the published
OCI image, built with a host Docker/Podman when one is present; and agent
Dockerfiles (and overlays on hosts with no engine), which build **inside the
persistent per-host orchestrator VM** (`infra_vm.py`) with buildah, so no host
Docker daemon or root-equivalent `docker` group is required. In-VM builds run
over SSH and stream the rootfs back to the host.

Builds run on the control-plane (orchestrator) side — not the data-plane
gateway — per PRD 0070 v1 ("builds stay in the orchestrator"): it owns
launches and already carries buildah. Tradeoff: an untrusted Dockerfile's
`RUN` steps share the VM with the control plane (buildah `--isolation chroot`
isn't a hard boundary) — the accepted blast-radius tradeoff, re-splittable
into a disposable builder (booted from this same image on its own TAP) later.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Callable
from typing import Generator

from ... import agent_overlay, image_registry, resources
from .. import container_engine
from ...contract.hostctl import InfraSettings, Settings
from ...image_registry import TOKEN_ENV as _ARTIFACT_TOKEN_ENV
from ...image_registry import RegistryError
from ...lib.log import die, info
from ...manifest import ManifestImageOverlay
from ...paths import host_artifact_cache_dir
from ...published_artifacts import FORMAT_EXT4, Artifact
from . import util
from .infra import FirecrackerInfraService
from . import infra_vm
from .infra_vm import GUEST_BUILD_STORE
from .orchestrator import build_store_volume_path

# overlay + chroot: buildah works as root in the microVM on the guest kernel's
# overlayfs (vfs copied every layer in full, ~8x a large image, and filled the
# VM disk). The store root is the orchestrator's persistent build-store volume,
# so pulled bases survive VM restarts (and an older vfs store never trips
# buildah's driver-mismatch check). `--isolation` is a build/run-only flag;
# `from`/`mount` take just the store. Every command that writes the store
# ends in `sync`: the infra VM is stopped by killing it, and unflushed layer
# metadata comes back empty and breaks the next mount.
_STORE_FLAG = ("--storage-driver overlay"
               f" --root {GUEST_BUILD_STORE}"
               " --runroot /run/containers/overlay-storage")
_BUILD_FLAGS = f"--isolation chroot {_STORE_FLAG}"

_BUILD_TIMEOUT_SECONDS = 900.0
_SMOKE_TIMEOUT_SECONDS = 120.0

# Below this much free space in the build store, drop every image before a
# build. The store is a cache: a wipe costs one re-pull, while a full store
# fails the build.
_STORE_MIN_FREE_MIB = 4096
_PRUNE_LOW_STORE = (
    f"[ \"$(df -Pm {GUEST_BUILD_STORE} | awk 'NR==2 {{print $4}}')\" -ge "
    f"{_STORE_MIN_FREE_MIB} ] || buildah rmi -a -f {_STORE_FLAG} >/dev/null 2>&1 || true; "
)


def _dockerfile_hash(dockerfile: Path) -> str:
    """The Dockerfile's content hash. Agent Dockerfiles mostly COPY nothing
    from the build context, so their text nearly determines the built image;
    any files they *do* COPY are folded into `_rootfs_digest` (so a changed
    input busts the cache) and shipped to the VM-side context by
    `_send_build_context`."""
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]


def _context_copy_sources(dockerfile: Path) -> list[str]:
    """The build-context-relative paths a Dockerfile ``COPY``s or ``ADD``s in.

    Agent Dockerfiles are meant to pull nothing from the context (the VM-side
    build ships only the Dockerfile), but one may pin an input by COPYing or
    ADDing a committed file — a checksum list, an npm lockfile. Return those
    source paths so the builder can both ship them to the VM context and fold
    them into the cache key. ``COPY --from=<stage>`` reads a build stage, not
    the context, so it is excluded; an ``ADD`` of a remote URL or git ref is
    fetched by the builder, not shipped from the context, so it is excluded too;
    the JSON/exec form is unused by the shipped images and is skipped rather
    than mis-parsed."""
    joined = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    sources: list[str] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not re.match(r"(?i)^(COPY|ADD)\s", stripped):
            continue
        tokens = stripped.split()[1:]
        if any(t.startswith("--from=") for t in tokens):
            continue
        args = [t for t in tokens if not t.startswith("--")]
        if len(args) < 2 or args[0].startswith("["):
            continue
        # ADD sources may be remote (URL / git ref); those are not context files.
        sources.extend(
            a for a in args[:-1] if not re.match(r"(?i)^(https?|git)://", a)
        )
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


def _boot_bits_hash(infra: InfraSettings) -> str:
    """The guest boot bits written into every agent rootfs: the init script and
    the static dropbear. Part of every agent cache key, so a change to either
    re-prepares the rootfs instead of reusing one that carries the old bits."""
    h = hashlib.sha256(util._GUEST_INIT.encode())
    h.update(b"\0")
    h.update(hashlib.sha256(util.dropbear_path(infra).read_bytes()).digest())
    return h.hexdigest()


def _rootfs_digest(dockerfile: Path, infra: InfraSettings) -> str:
    """Cache key for a rootfs built from `dockerfile`: the Dockerfile, the
    centralized build args, the guest boot bits, and the content of any files
    the Dockerfile COPYs from the build context (so a repinned input, e.g. a
    changed npm lockfile, rebuilds)."""
    h = hashlib.sha256()
    h.update(_dockerfile_hash(dockerfile).encode())
    h.update(b"\0")
    for name, value in resources.image_build_args(dockerfile).items():
        h.update(name.encode())
        h.update(b"=")
        h.update(value.encode())
        h.update(b"\0")
    h.update(_boot_bits_hash(infra).encode())
    for rel, path in _context_files(dockerfile):
        h.update(b"\0")
        h.update(rel.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _rootfs_dir(name: str) -> Path:
    return util.cache_dir() / "rootfs" / name


def _ready_dir(name: str) -> Path | None:
    base = _rootfs_dir(name)
    return base if (base / ".bb-ready").is_file() else None


def _force_rmtree(path: Path) -> None:
    """Remove a rootfs tree, including read-only directories an exported image
    may carry (a 0555 dir can't have its entries unlinked until it is made
    writable). Symlinks are never followed."""
    if path.is_symlink() or not path.exists():
        path.unlink(missing_ok=True)
        return
    for dirpath, dirnames, _ in os.walk(path):
        os.chmod(dirpath, 0o755)
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            if not os.path.islink(sub):
                os.chmod(sub, 0o755)
    shutil.rmtree(path)


def _publish_rootfs_dir(
    name: str, build: Callable[[Path], None], infra: InfraSettings,
) -> Path:
    """The cached rootfs dir `name`, built on a miss: `build(staging)` fills a
    staging dir, the guest boot bits are injected, and the result is published
    by atomic rename, all under the host build lock (the infra VM's buildah
    store and this cache dir are shared between launches). A failed build
    leaves nothing behind; a partial build is never visible under `name`."""
    ready = _ready_dir(name)
    if ready is not None:
        info(f"using cached agent rootfs {name}")
        return ready
    with _build_lock():
        ready = _ready_dir(name)
        if ready is not None:  # another launch built it while we waited
            info(f"using cached agent rootfs {name}")
            return ready
        base = _rootfs_dir(name)
        staging = base.with_name(f".staging-{name}")
        _force_rmtree(staging)
        staging.mkdir(parents=True)
        try:
            build(staging)
            util.inject_guest_boot(staging, infra)
            (staging / ".bb-ready").write_text("ok\n")
            _force_rmtree(base)
            os.rename(staging, base)
        finally:
            _force_rmtree(staging)
    return base


def cached_agent_rootfs_dir(dockerfile: Path, infra: InfraSettings) -> Path | None:
    """Return the ready cached rootfs for ``dockerfile``, if one exists."""
    return _ready_dir(f"agent-{_rootfs_digest(dockerfile, infra)}")


# Free space added to a pulled agent image beyond what it was published with,
# matching the slack a directory-built bottle disk gets (`build_rootfs_ext4`).
_PULLED_IMAGE_SLACK_MIB = 1024


def _image_ext4_path(artifact: Artifact, infra: InfraSettings) -> Path:
    """Where the prepared ext4 base for published `artifact` is cached, keyed by
    the published file's digest plus this host's guest boot bits."""
    h = hashlib.sha256(f"{artifact.digest}\0{_boot_bits_hash(infra)}".encode())
    return _rootfs_dir(f"agent-ext4-{h.hexdigest()[:16]}.ext4")


def cached_agent_image_ext4(artifact: Artifact, infra: InfraSettings) -> Path | None:
    """A prepared ext4 base previously made from published `artifact`, or None."""
    image = _image_ext4_path(artifact, infra)
    return image if image.with_suffix(".ready").is_file() else None


def acquire_agent_image_ext4(artifact: Artifact, settings: Settings) -> Path:
    """The bootable agent base for a published ext4 `artifact` (PRD 0085):
    download and verify it on the host (`image_registry.ensure_ext4`, digest
    checked, fail closed), decompress it, add slack, and write this host's
    guest boot bits into it. Prepared once and cached; each bottle then gets a
    sparse copy (`util.make_bottle_rootfs`)."""
    if artifact.format != FORMAT_EXT4:
        die(f"firecracker agent base must be an ext4 artifact, got {artifact.format!r}")
    infra = settings.infra
    image = _image_ext4_path(artifact, infra)
    ready = image.with_suffix(".ready")
    if ready.is_file():
        info(f"using cached agent rootfs {image.name}")
        return image
    with _build_lock():
        if ready.is_file():  # another launch prepared it while we waited
            info(f"using cached agent rootfs {image.name}")
            return image
        try:
            gz = image_registry.ensure_ext4(
                artifact, cache_root=host_artifact_cache_dir(infra.artifact_cache))
        except RegistryError as e:
            die(str(e))
        staging = image.with_suffix(".part")
        staging.parent.mkdir(parents=True, exist_ok=True)
        info(f"preparing published agent rootfs {artifact.image} ({artifact.digest[:19]}…)")
        try:
            with gzip.open(gz, "rb") as src, open(staging, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            # Grow first: debugfs can write a truncated file into a full image.
            util.grow_ext4(staging, _PULLED_IMAGE_SLACK_MIB)
            util.inject_guest_boot_ext4(staging, infra)
            staging.replace(image)
            ready.write_text("ok\n")
        finally:
            staging.unlink(missing_ok=True)
    return image


def _overlay_rootfs_name(staged: agent_overlay.StagedOverlay, infra: InfraSettings) -> str:
    h = hashlib.sha256(f"{staged.cache_key}\0{_boot_bits_hash(infra)}".encode())
    return f"agent-overlay-{h.hexdigest()[:16]}"


def cached_agent_overlay_rootfs_dir(
    artifact: Artifact, overlay: ManifestImageOverlay, infra: InfraSettings,
    *, template: str, source_root: Path,
) -> Path | None:
    """A ready rootfs previously built for `overlay` on the pinned OCI
    `artifact`, or None."""
    with agent_overlay.staged_overlay(
        image_registry.pinned_reference(artifact), overlay,
        template=template, source_root=source_root,
    ) as staged:
        return _ready_dir(_overlay_rootfs_name(staged, infra))


def acquire_agent_overlay_rootfs_dir(
    artifact: Artifact,
    overlay: ManifestImageOverlay,
    settings: Settings,
    *,
    template: str,
    source_root: Path,
    smoke_test: tuple[str, ...] = (),
) -> Path:
    """The rootfs of `overlay` built as a thin layer on the pinned OCI
    `artifact` (PRD 0085 tier 2), cached by the staged build context and the
    guest boot bits. Builds with a host Docker/Podman when one is present (it
    shares the user's layer cache), else in the infra VM. The pin's tag is
    checked against its digest only when building, so a cached overlay
    launches offline. `source_root` is where the overlay's inputs resolve from
    (the launch cwd)."""
    base_ref = image_registry.pinned_reference(artifact)
    with agent_overlay.staged_overlay(
        base_ref, overlay, template=template, source_root=source_root,
    ) as staged:
        name = _overlay_rootfs_name(staged, settings.infra)
        if _ready_dir(name) is None:
            try:
                image_registry.verify_tag(artifact)
                engine = container_engine.host_container_engine(settings.infra.build_engine)
            except (RegistryError, container_engine.EngineUnavailable) as e:
                die(str(e))
        else:
            engine = None
        tag = f"bot-bottle-agent-overlay-{staged.cache_key}"

        def build(out: Path) -> None:
            if engine is not None:
                info(f"building overlay agent image on {base_ref!r} with host {engine}")
                _build_overlay_on_host(engine, base_ref, staged, tag, out, smoke_test)
                return
            info(f"building overlay agent image on {base_ref!r} in the infra VM")
            _infra_build(
                dockerfile=staged.dockerfile, ctx_root=staged.context,
                ctx_rels=sorted(p.name for p in staged.context.iterdir()
                                if p != staged.dockerfile),
                tag=tag, build_args={}, smoke_test=smoke_test, out=out,
                settings=settings, registry=base_ref.split("/", 1)[0],
            )

        return _publish_rootfs_dir(name, build, settings.infra)


def build_agent_rootfs_dir(
    dockerfile: Path, settings: Settings, *, image_tag: str, smoke_test: tuple[str, ...] = (),
) -> Path:
    """Build `dockerfile` in the infra VM (buildah, no host docker), export its
    rootfs, inject the guest boot bits, and return the cached base dir — the
    same shape `util.build_rootfs_ext4` consumes. Cached by `_rootfs_digest`,
    so a repeat launch skips the rebuild but an input change rebuilds.

    `smoke_test` (the provider's declared argv, e.g. `("claude","--version")`)
    is run in the freshly built image before export, catching an npm
    silent-failure image at build time rather than at first agent use."""
    digest = _rootfs_digest(dockerfile, settings.infra)

    def build(out: Path) -> None:
        info(f"building agent image {image_tag!r} in the infra VM")
        _infra_build(
            dockerfile=dockerfile, ctx_root=resources.build_root(),
            ctx_rels=[rel for rel, _ in _context_files(dockerfile)],
            tag=f"bot-bottle-agent-build-{digest}",
            build_args=resources.image_build_args(dockerfile),
            smoke_test=smoke_test, out=out, settings=settings,
        )

    return _publish_rootfs_dir(f"agent-{digest}", build, settings.infra)


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


def _infra_build(
    *,
    dockerfile: Path,
    ctx_root: Path,
    ctx_rels: list[str],
    tag: str,
    build_args: dict[str, str],
    smoke_test: tuple[str, ...],
    out: Path,
    settings: Settings,
    registry: str = "",
) -> None:
    """`buildah build` `dockerfile` in the orchestrator VM, with the paths
    `ctx_rels` (under `ctx_root`) as its context, smoke-test the image, and
    stream its rootfs into `out`. With `registry` and an artifact token,
    buildah logs in first (token on stdin) so a FROM can pull a private base.
    The infra VMs persist; only this build's containers, image, login and
    context are cleaned up, never another build's."""
    service = FirecrackerInfraService(settings)
    service.ensure_running()
    key, ip = service.orchestrator().ssh_target()
    ctx = f"/tmp/{tag}"
    smoke_ctr, export_ctr = f"{tag}-smoke", f"{tag}-export"
    token = os.environ.get(_ARTIFACT_TOKEN_ENV, "").strip() if registry else ""
    logout = f"buildah logout {shlex.quote(registry)} >/dev/null 2>&1; " if token else ""

    def _cleanup() -> None:
        _ssh(key, ip,
             f"buildah rm {_STORE_FLAG} {smoke_ctr} {export_ctr} >/dev/null 2>&1; "
             f"buildah rmi {_STORE_FLAG} {tag} >/dev/null 2>&1; "
             f"{logout}rm -rf {ctx}; sync",
             timeout=60)

    _cleanup()  # clear leftovers from a crashed prior build of this tag
    try:
        prep = _ssh(key, ip, f"mkdir -p {ctx}/ctx")
        if prep.returncode != 0:
            die(f"preparing the build dir in the infra VM failed: {prep.stderr.strip()}")
        if token:
            # Gitea accepts any username with a valid token as the password.
            login = _ssh(
                key, ip,
                f"buildah login --username bot-bottle --password-stdin {shlex.quote(registry)}",
                input=token, timeout=120,
            )
            if login.returncode != 0:
                die(f"registry login in the infra VM failed: {login.stderr.strip()}")
        _send_dockerfile(key, ip, dockerfile, ctx)
        _tar_into_vm(key, ip, ctx_root, ctx_rels, f"{ctx}/ctx")
        _buildah_build(key, ip, ctx, tag, build_args)
        _smoke_test(key, ip, tag, smoke_ctr, smoke_test)
        _stream_rootfs(key, ip, tag, export_ctr, out)
    finally:
        _cleanup()


def _build_overlay_on_host(
    engine: str,
    base_ref: str,
    staged: agent_overlay.StagedOverlay,
    tag: str,
    out: Path,
    smoke_test: tuple[str, ...],
) -> None:
    """Build the staged overlay with a host container engine, smoke-test it,
    and unpack its rootfs into `out`. With an artifact token the private base
    is pulled first with throwaway credentials (`_pull_with_token`); the build
    and later steps then use the engine as the user has it configured."""
    token = os.environ.get(_ARTIFACT_TOKEN_ENV, "").strip()
    try:
        if token:
            _pull_with_token(engine, base_ref, token)
        build = subprocess.run(
            [engine, "build", "-f", str(staged.dockerfile), "-t", tag, str(staged.context)],
            timeout=_BUILD_TIMEOUT_SECONDS, check=False,
        )
        if build.returncode != 0:
            die(f"{engine} build of the overlay on {base_ref!r} failed "
                f"(exit {build.returncode})")
        if smoke_test:
            smoke = subprocess.run(
                [engine, "run", "--rm", "--entrypoint", smoke_test[0], tag, *smoke_test[1:]],
                capture_output=True, text=True, timeout=_SMOKE_TIMEOUT_SECONDS, check=False,
            )
            if smoke.returncode != 0:
                die(f"overlay agent image failed its smoke test ({' '.join(smoke_test)}): "
                    f"{(smoke.stderr or smoke.stdout).strip()}")
        util.export_image_rootfs(engine, tag, out)
    except subprocess.TimeoutExpired as e:
        die(f"{engine} timed out after {e.timeout:.0f}s building the overlay on {base_ref!r}")
    finally:
        subprocess.run([engine, "rmi", "-f", tag],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _pull_with_token(engine: str, ref: str, token: str) -> None:
    """Pull `ref` with the artifact token held in a throwaway credentials
    location (`docker --config` / `podman --authfile`), so the user's own
    registry login is neither used nor changed. A separate `--config` also
    drops the user's current Docker context, so the pull is pointed at that
    context's daemon explicitly, the one the build will use."""
    registry = ref.split("/", 1)[0]
    with tempfile.TemporaryDirectory(prefix="bb-auth-") as auth:
        if engine == "docker":
            host = subprocess.run(
                ["docker", "context", "inspect", "-f", "{{.Endpoints.docker.Host}}"],
                capture_output=True, text=True, timeout=_SMOKE_TIMEOUT_SECONDS, check=False,
            ).stdout.strip()
            client = ["docker", "--config", auth, *(["-H", host] if host else [])]
            login = [*client, "login", "--username", "bot-bottle", "--password-stdin", registry]
            pull = [*client, "pull", ref]
        else:
            authfile = str(Path(auth) / "auth.json")
            login = [engine, "login", "--authfile", authfile, "--username", "bot-bottle",
                     "--password-stdin", registry]
            pull = [engine, "pull", "--authfile", authfile, ref]
        result = subprocess.run(login, input=token, capture_output=True, text=True,
                                timeout=_SMOKE_TIMEOUT_SECONDS, check=False)
        if result.returncode != 0:
            die(f"{engine} login to {registry} failed: {result.stderr.strip()}")
        result = subprocess.run(pull, capture_output=True, text=True,
                                timeout=_BUILD_TIMEOUT_SECONDS, check=False)
        if result.returncode != 0:
            die(f"{engine} pull of {ref!r} failed: {(result.stderr or result.stdout).strip()}")


def _ssh(private_key: Path, guest_ip: str, script: str,
         *, timeout: float = 60.0, input: str | None = None,
         ) -> subprocess.CompletedProcess[str]:
    # `input` is how secrets reach the guest: never in `script`, which is an
    # argument to the host's ssh and visible in `ps`.
    return subprocess.run(
        util.ssh_base_argv(private_key, guest_ip) + [script],
        input=input, capture_output=True, text=True, timeout=timeout, check=False,
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


def _tar_into_vm(
    private_key: Path, guest_ip: str, root: Path, rels: list[str], dest: str,
) -> None:
    """Stream the paths `rels` (relative to `root`, directories recursively,
    symlinks as links) into the infra VM's `dest`, preserving their relative
    paths. A no-op for an empty list: most agent Dockerfiles COPY nothing, so
    the context stays empty."""
    if not rels:
        return
    tar = subprocess.Popen(
        ["tar", "-C", str(root), "-cf", "-", "--", *rels],
        stdout=subprocess.PIPE,
    )
    try:
        proc = subprocess.run(
            util.ssh_base_argv(private_key, guest_ip) + [f"tar -C {dest} -xf -"],
            stdin=tar.stdout, capture_output=True, timeout=120, check=False,
        )
    finally:
        if tar.stdout is not None:
            tar.stdout.close()
        tar.wait()
    if tar.returncode != 0:
        die(f"packing the build context failed (tar exit {tar.returncode})")
    if proc.returncode != 0:
        die("sending the build context to the infra VM failed: "
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
        f"mkdir -p {GUEST_BUILD_STORE}; {_PRUNE_LOW_STORE}"
        f"buildah build {_BUILD_FLAGS} {arg_flags} "
        f"-t {tag} -f {ctx}/Dockerfile {ctx}/ctx; rc=$?; sync; exit $rc",
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if rc != 0:
        die(f"buildah build in the infra VM failed (exit {rc}); "
            "see the build output above.\n"
            "If it shows a storage error (e.g. 'layer not known', or a readlink "
            "under overlay/), the cached build store may be damaged. With no "
            "bottles running, stop the orchestrator VM and delete the store; the "
            "next launch recreates it:\n"
            f"    kill $(cat {infra_vm._pid_file(infra_vm._orch_dir())})\n"
            f"    rm {build_store_volume_path()}")


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
        f"buildah rm {_STORE_FLAG} {ctr} >/dev/null 2>&1 || true; sync; exit $rc"
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
    # --no-overwrite-dir: the stream's `.` entry carries the mount root's mode
    # (0555 on overlay), which would leave `base` unwritable for the guest
    # boot bits injected next.
    untar = subprocess.run(
        ["tar", "-x", "--no-overwrite-dir", "-C", str(base)],
        stdin=ssh_proc.stdout, check=False,
    )
    ssh_proc.stdout.close()
    ssh_err = (ssh_proc.stderr.read().decode(errors="replace")
               if ssh_proc.stderr else "")
    rc = ssh_proc.wait()
    if rc != 0 or untar.returncode != 0:
        die(f"exporting the built rootfs from the infra VM failed: "
            f"{ssh_err.strip() or '<no stderr>'}")
