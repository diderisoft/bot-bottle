"""Docker host primitives: `run_docker`, image and container checks, builds."""

from __future__ import annotations

import os

from datetime import datetime, timezone
import shutil
import subprocess


from ... import resources
from ...lib.log import die, info
from ...lib.proc import run_streaming


def run_docker(
    argv: list[str], *, env: dict[str, str] | None = None, input: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a `docker` command, capturing text output. Never raises on non-zero
    exit. `env` is the child env, for handing a secret to a bare `--env NAME`."""
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        check=False, env=env, input=input, timeout=timeout,
    )


_DAEMON_PROBE_TIMEOUT_SECONDS = 10


def daemon_running() -> bool:
    """True iff the docker daemon answers. Bottles only run on a local daemon,
    so no answer means nothing is running. A hung probe counts as running."""
    try:
        proc = run_docker(["docker", "version", "--format", "{{.Server.Version}}"],
                          timeout=_DAEMON_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False
    return proc.returncode == 0




def isolation_note(use_runsc: bool) -> str:
    """One line on the docker backend's boundary for `start` and `doctor`, or
    "" when gVisor narrows it."""
    if use_runsc:
        return ""
    return ("docker backend: agents are containers sharing the host kernel, the "
            "weakest of the three backends. Firecracker (Linux with KVM) or Apple "
            "Container (macOS) isolate better; registering gVisor (runsc) with "
            "Docker narrows the gap.")


def runsc_available() -> bool:
    """True if Docker has the gVisor `runsc` runtime registered."""
    r = run_docker(["docker", "info", "--format", "{{json .Runtimes}}"])
    return r.returncode == 0 and "runsc" in r.stdout


def require_docker() -> None:
    """Fail with an install pointer if `docker` isn't on PATH."""
    if shutil.which("docker") is None:
        info("Docker is required but was not found on PATH.")
        info("macOS:  install Docker Desktop  https://docs.docker.com/desktop/install/mac-install/")
        info("Linux:  install Docker Engine   https://docs.docker.com/engine/install/")
        die("docker not found")


def image_exists(ref: str) -> bool:
    return run_docker(["docker", "image", "inspect", ref]).returncode == 0


def tag_image(src: str, dst: str) -> None:
    """Tag local image `src` as `dst` too."""
    result = run_docker(["docker", "image", "tag", src, dst])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(f"could not tag {src} as {dst}: {detail or '<no detail>'}")


def container_exists(name: str) -> bool:
    """True if a container named exactly `name` exists, running or stopped."""
    result = run_docker(["docker", "ps", "-a", "-q", "-f", f"name=^{name}$"])
    return result.returncode == 0 and bool(result.stdout.strip())


def force_remove_container(name: str) -> None:
    """`docker rm -f` the container if it exists. Best effort."""
    if container_exists(name):
        run_docker(["docker", "rm", "-f", name])


def build_image(
    ref: str,
    context: str,
    *,
    dockerfile: str = "",
    build_args: dict[str, str] | None = None,
    no_cache: bool = False,
) -> None:
    """`docker build`, every call (the layer cache keeps it cheap).
    `dockerfile` is an optional path relative to `context`. `no_cache` forces
    `--no-cache`, for layers a flaky installer cached broken."""
    info(f"building image {ref} from {context} (layer cache keeps repeat builds fast)")
    args = ["docker", "build", "-t", ref]
    if no_cache:
        args.append("--no-cache")
    if dockerfile:
        # `docker build` resolves -f against the cwd, not the context, and the
        # host controller runs from its state dir.
        path = dockerfile if os.path.isabs(dockerfile) else os.path.join(context, dockerfile)
        args.extend(["-f", path])
    effective_build_args = resources.image_build_args(
        dockerfile,
        context=context,
    ) if dockerfile else {}
    effective_build_args.update(build_args or {})
    for name, value in effective_build_args.items():
        args.extend(["--build-arg", f"{name}={value}"])
    args.append(context)
    # Streamed so a host-controller job captures the output.
    run_streaming(args, check=True)


def image_id(ref: str) -> str:
    """The content-addressed ID of a local image, so a base can't change
    between builds.
    """
    result = run_docker(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
    image = result.stdout.strip()
    if result.returncode != 0 or not image.startswith("sha256:"):
        detail = (result.stderr or result.stdout or "").strip()
        die(f"could not resolve exact image ID for {ref!r}: {detail or '<no detail>'}")
    return image


def pinned_local_image_ref(ref: str) -> str:
    """Tag a local image with a content-derived tag and check it resolves back
    to the same ID. BuildKit reads a bare `sha256:...` in `FROM` as a
    repository.
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
    """Run `argv` in a throwaway container of a fresh image and die if it
    fails. No-op without a provider smoke test."""
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




def commit_container(container_name: str, image_tag: str) -> None:
    """`docker commit` the container to `image_tag`."""
    result = run_docker(["docker", "commit", container_name, image_tag])
    if result.returncode != 0:
        die(
            f"docker commit {container_name!r} → {image_tag!r} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    info(f"committed {container_name!r} → {image_tag!r}")


def image_created_at(ref: str) -> datetime | None:
    """The image's Created timestamp as aware UTC, or None (skip the stale
    check)."""
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
