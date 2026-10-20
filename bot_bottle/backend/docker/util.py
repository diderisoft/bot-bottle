"""Docker host-side primitives used by DockerBottleBackend: the lean
`run_docker` subprocess wrapper, probing for docker on PATH, slugifying agent
names, checking image/container existence, and building images."""

from __future__ import annotations

from datetime import datetime, timezone
import shutil
import subprocess
from typing import Iterator

from ... import resources
from ...lib.log import die, info
from ...lib.proc import run_streaming
from ...util import slugify as _slugify


def slugify(name: str) -> str:
    """Compatibility wrapper; new generic callers import ``bot_bottle.util``."""
    return _slugify(name)


def run_docker(
    argv: list[str], *, env: dict[str, str] | None = None, input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a `docker` command, capturing stdout/stderr as text. Never raises on
    a non-zero exit — callers inspect `returncode` / `stderr` so they can stay
    fail-closed or tolerate idempotent no-ops (e.g. removing an already-absent
    container).

    `env` sets the child process environment — used to hand a secret to a bare
    `--env NAME` flag (docker inherits its value from this process) so the value
    never lands on argv or in `docker inspect`'s recorded command line."""
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        check=False, env=env, input=input, timeout=timeout,
    )


_DAEMON_PROBE_TIMEOUT_SECONDS = 10


def daemon_running() -> bool:
    """True iff the docker daemon answers. bot-bottle's docker containers
    bind-mount host paths, so they only ever run on a local daemon: when it
    doesn't answer, no bottle or gateway is running. A probe that hangs counts
    as running, so callers don't claim an empty host they couldn't check."""
    try:
        proc = run_docker(["docker", "version", "--format", "{{.Server.Version}}"],
                          timeout=_DAEMON_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False
    return proc.returncode == 0


# Cap on the suffix the container-name conflict logic will try before
# giving up: base, base-2, ..., base-MAX_CONTAINER_SUFFIX.
MAX_CONTAINER_SUFFIX = 100


def container_name_candidates(base: str) -> Iterator[str]:
    """Yield `base`, then `base-2`, `base-3`, ... up to
    `base-MAX_CONTAINER_SUFFIX`. Both the prepare-time probe and the
    launch-time race retry walk this sequence."""
    yield base
    for suffix in range(2, MAX_CONTAINER_SUFFIX + 1):
        yield f"{base}-{suffix}"


def runsc_available() -> bool:
    """Return True if the Docker daemon has the gVisor (`runsc`) runtime
    registered. Called once per prepare; the result lives on the plan."""
    r = run_docker(["docker", "info", "--format", "{{json .Runtimes}}"])
    return r.returncode == 0 and "runsc" in r.stdout


def require_docker() -> None:
    """Fail with an install pointer if `docker` is not on PATH."""
    if shutil.which("docker") is None:
        info("Docker is required but was not found on PATH.")
        info("macOS:  install Docker Desktop  https://docs.docker.com/desktop/install/mac-install/")
        info("Linux:  install Docker Engine   https://docs.docker.com/engine/install/")
        die("docker not found")


def image_exists(ref: str) -> bool:
    return run_docker(["docker", "image", "inspect", ref]).returncode == 0


def tag_image(src: str, dst: str) -> None:
    """Give local image `src` the additional tag `dst`, so a pulled pinned image
    substitutes for a locally-built one everywhere downstream refers to `dst`."""
    result = run_docker(["docker", "image", "tag", src, dst])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(f"could not tag {src} as {dst}: {detail or '<no detail>'}")


def container_exists(name: str) -> bool:
    """Returns True if a container (running or stopped) with the given
    name exists. Uses `docker ps -a -q -f name=^<name>$` so substring
    matches don't false-positive."""
    result = run_docker(["docker", "ps", "-a", "-q", "-f", f"name=^{name}$"])
    return result.returncode == 0 and bool(result.stdout.strip())


def force_remove_container(name: str) -> None:
    """`docker rm -f` the named container if it exists. No-op if it
    doesn't — and the rm itself is best-effort (errors swallowed) so
    this is safe to register as a teardown callback."""
    if container_exists(name):
        run_docker(["docker", "rm", "-f", name])


def docker_exec_root(container: str, argv: list[str]) -> None:
    """Run `docker exec -u 0` in the named container, check=True. Used
    by SSH provisioning to chown/chmod files that need root."""
    subprocess.run(
        ["docker", "exec", "-u", "0", container, *argv],
        stdout=subprocess.DEVNULL,
        check=True,
    )


def docker_exec(container: str, argv: list[str], *, user: str = "") -> None:
    """Run `docker exec` in the named container, dying with the
    command's own stderr on failure. Pass `user=\"0\"` to run as root."""
    cmd = ["docker", "exec"]
    if user:
        cmd += ["-u", user]
    cmd += [container, *argv]
    result = run_docker(cmd)
    if result.returncode != 0:
        die(
            f"docker exec in {container} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def docker_cp(src: str, dest: str) -> None:
    """Run `docker cp`, dying with the command's own stderr on failure."""
    result = run_docker(["docker", "cp", src, dest])
    if result.returncode != 0:
        die(f"docker cp {src} -> {dest} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}")


def build_image(
    ref: str,
    context: str,
    *,
    dockerfile: str = "",
    build_args: dict[str, str] | None = None,
    no_cache: bool = False,
) -> None:
    """Invokes `docker build` every call. Layer cache makes no-change
    rebuilds cheap; running every time means Dockerfile edits land
    without manual `docker rmi`.

    `dockerfile` is an optional path (relative to `context`, or
    absolute) for callers that need to build from a non-default
    Dockerfile in the same context — e.g. `Dockerfile.git-gate`.

    `no_cache` (the `start --no-cache` flag) forces `--no-cache`. The
    npm/curl installers some provider Dockerfiles
    shell out to can silently no-op on a transient network failure —
    e.g. an `optionalDependencies` fetch for a platform-native binary —
    and Docker will then cache that broken layer indefinitely."""
    info(f"building image {ref} from {context} (layer cache keeps repeat builds fast)")
    args = ["docker", "build", "-t", ref]
    if no_cache:
        args.append("--no-cache")
    if dockerfile:
        args.extend(["-f", dockerfile])
    effective_build_args = resources.image_build_args(
        dockerfile,
        context=context,
    ) if dockerfile else {}
    effective_build_args.update(build_args or {})
    for name, value in effective_build_args.items():
        args.extend(["--build-arg", f"{name}={value}"])
    args.append(context)
    # Streamed, so a host controller job captures the build's output.
    run_streaming(args, check=True)


def image_id(ref: str) -> str:
    """Return the exact content-addressed ID for a local image.

    This is used when one locally built image is another Dockerfile's base:
    passing the ID prevents a mutable tag from being resolved between builds.
    """
    result = run_docker(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
    image = result.stdout.strip()
    if result.returncode != 0 or not image.startswith("sha256:"):
        detail = (result.stderr or result.stdout or "").strip()
        die(f"could not resolve exact image ID for {ref!r}: {detail or '<no detail>'}")
    return image


def pinned_local_image_ref(ref: str) -> str:
    """Give a local image a content-derived tag and verify the tag resolves
    back to the same image ID.

    BuildKit treats a bare ``sha256:...`` ID in ``FROM`` as a registry
    repository name. A tag whose complete suffix is the local image ID remains
    resolvable by BuildKit, while the post-tag inspection keeps the handoff
    fail-closed.
    """
    image = image_id(ref)
    digest = image.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        die(f"could not derive a local base tag from invalid image ID {image!r}")

    repository = ref.split("@", 1)[0]
    last_slash = repository.rfind("/")
    last_colon = repository.rfind(":")
    if last_colon > last_slash:
        repository = repository[:last_colon]
    pinned_ref = f"{repository}:sha256-{digest}"

    result = run_docker(["docker", "image", "tag", image, pinned_ref])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(f"could not tag exact local image {image}: {detail or '<no detail>'}")
    if image_id(pinned_ref) != image:
        die(f"content-derived local tag {pinned_ref!r} did not resolve to {image}")
    return pinned_ref


def verify_agent_image(image: str, argv: tuple[str, ...]) -> None:
    """Run `argv` inside a throwaway container of a freshly built agent
    image and die loudly if it fails, instead of shipping an image
    whose CLI only breaks at first real use. No-op when the provider
    hasn't declared a smoke test (`AgentProviderRuntime.smoke_test`)."""
    if not argv:
        return
    result = run_docker(["docker", "run", "--rm", "--entrypoint", argv[0], image, *argv[1:]])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(
            f"agent image {image!r} failed its post-build smoke test "
            f"({' '.join(argv)}): {detail}\n"
            f"Try rebuilding from scratch: bot-bottle start --no-cache"
        )


# def build_image_with_cwd(
#     derived: str,
#     base: str,
#     workspace: "WorkspacePlan",
# ) -> None:
#     """Build a thin derived image that copies the workspace into
#     the plan's guest path and sets the plan's workdir."""
#     import os
#
#     cwd = str(workspace.host_path)
#     if not os.path.isdir(cwd):
#         die(f"cwd not found at {cwd}")
#     info(f"building image {derived} from {base} with {cwd} -> {workspace.guest_path}")
#     with tempfile.TemporaryDirectory(prefix="bot-bottle-cwd.") as tmp:
#         context_dir = os.path.join(tmp, "context")
#         staged_workspace = os.path.join(context_dir, "workspace")
#         shutil.copytree(
#             cwd,
#             staged_workspace,
#             symlinks=True,
#             ignore=shutil.ignore_patterns(".git"),
#         )
#         dockerfile = (
#             f"FROM {base}\n"
#             f"COPY --chown=node:node workspace/. {workspace.guest_path}\n"
#             f"WORKDIR {workspace.workdir}\n"
#         )
#         subprocess.run(
#             ["docker", "build", "-t", derived, "-f", "-", context_dir],
#             input=dockerfile,
#             text=True,
#             check=True,
#         )


def commit_container(container_name: str, image_tag: str) -> None:
    """Run `docker commit <container_name> <image_tag>` to snapshot the
    running container's filesystem state as a local Docker image."""
    result = run_docker(["docker", "commit", container_name, image_tag])
    if result.returncode != 0:
        die(
            f"docker commit {container_name!r} → {image_tag!r} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    info(f"committed {container_name!r} → {image_tag!r}")


def image_created_at(ref: str) -> datetime | None:
    """Return Docker's image Created timestamp as an aware UTC datetime, or
    None when the field is absent or unparseable. Callers should skip the
    stale check when None is returned."""
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Created}}", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(
            f"docker image inspect for {ref!r} failed: "
            f"{(r.stderr or '').strip() or '<no stderr>'}"
        )
    raw = r.stdout.strip()
    if not raw:
        return None
    try:
        return _parse_docker_timestamp(raw)
    except ValueError:
        return None


def _parse_docker_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dot = text.find(".")
    if dot != -1:
        tz_plus = text.find("+", dot)
        tz_minus = text.find("-", dot)
        tz_candidates = [pos for pos in (tz_plus, tz_minus) if pos != -1]
        if tz_candidates:
            tz_pos = min(tz_candidates)
            frac = text[dot + 1:tz_pos]
            text = text[:dot + 1] + frac[:6].ljust(6, "0") + text[tz_pos:]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
