"""Docker host-side primitives used by DockerBottleBackend: probing
for docker on PATH, slugifying agent names, checking image/container
existence, and building images."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Iterable, Iterator

from ...log import die, info
# from ...workspace import WorkspacePlan


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
    r = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0 and "runsc" in r.stdout


def require_docker() -> None:
    """Fail with an install pointer if `docker` is not on PATH."""
    if shutil.which("docker") is None:
        info("Docker is required but was not found on PATH.")
        info("macOS:  install Docker Desktop  https://docs.docker.com/desktop/install/mac-install/")
        info("Linux:  install Docker Engine   https://docs.docker.com/engine/install/")
        die("docker not found")


def image_exists(ref: str) -> bool:
    return _silent_run(["docker", "image", "inspect", ref]) == 0


def container_exists(name: str) -> bool:
    """Returns True if a container (running or stopped) with the given
    name exists. Uses `docker ps -a -q -f name=^<name>$` so substring
    matches don't false-positive."""
    result = subprocess.run(
        ["docker", "ps", "-a", "-q", "-f", f"name=^{name}$"],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def force_remove_container(name: str) -> None:
    """`docker rm -f` the named container if it exists. No-op if it
    doesn't — and the rm itself is best-effort (errors swallowed) so
    this is safe to register as a teardown callback."""
    if container_exists(name):
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


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
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        die(
            f"docker exec in {container} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def docker_cp(src: str, dest: str) -> None:
    """Run `docker cp`, dying with the command's own stderr on failure."""
    result = subprocess.run(
        ["docker", "cp", src, dest], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"docker cp {src} -> {dest} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}")


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase, non-alnum runs → '-', trimmed. Dies on empty result."""
    if not name:
        die("slugify: missing name")
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        die(f"name '{name}' produced an empty slug; use alphanumeric characters")
    return slug


def build_image(ref: str, context: str, *, dockerfile: str = "") -> None:
    """Invokes `docker build` every call. Layer cache makes no-change
    rebuilds cheap; running every time means Dockerfile edits land
    without manual `docker rmi`.

    `dockerfile` is an optional path (relative to `context`, or
    absolute) for callers that need to build from a non-default
    Dockerfile in the same context — e.g. `Dockerfile.git-gate`.

    Set `BOT_BOTTLE_NO_CACHE=1` (the `start --no-cache` flag) to force
    `--no-cache`. The npm/curl installers some provider Dockerfiles
    shell out to can silently no-op on a transient network failure —
    e.g. an `optionalDependencies` fetch for a platform-native binary —
    and Docker will then cache that broken layer indefinitely."""
    info(f"building image {ref} from {context} (layer cache keeps repeat builds fast)")
    args = ["docker", "build", "-t", ref]
    if os.environ.get("BOT_BOTTLE_NO_CACHE") == "1":
        args.append("--no-cache")
    if dockerfile:
        args.extend(["-f", dockerfile])
    args.append(context)
    subprocess.run(args, check=True)


def verify_agent_image(image: str, argv: tuple[str, ...]) -> None:
    """Run `argv` inside a throwaway container of a freshly built agent
    image and die loudly if it fails, instead of shipping an image
    whose CLI only breaks at first real use. No-op when the provider
    hasn't declared a smoke test (`AgentProviderRuntime.smoke_test`)."""
    if not argv:
        return
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", argv[0], image, *argv[1:]],
        capture_output=True,
        text=True,
        check=False,
    )
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
    result = subprocess.run(
        ["docker", "commit", container_name, image_tag],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(
            f"docker commit {container_name!r} → {image_tag!r} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    info(f"committed {container_name!r} → {image_tag!r}")


def _silent_run(cmd: Iterable[str]) -> int:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode
