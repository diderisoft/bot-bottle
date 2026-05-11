"""Docker bottle factory.

`create_docker_bottle` owns the end-to-end Docker lifecycle:

  1. Probe whether gVisor (`runsc`) is registered with the daemon.
  2. Build the base image (and a per-cwd derived image if --cwd).
  3. Create the per-agent internal + egress networks.
  4. Boot the pipelock sidecar on both networks.
  5. Launch the agent container, with `--runtime=runsc` iff available.
  6. Copy the prompt, skills, SSH keys, and (optionally) .git into the
     running container.
  7. Yield a `Bottle` handle for `exec_claude` / `cp_in`.
  8. Tear everything down (container, sidecar, both networks) on exit.

The Bottle Protocol lives in `claude_bottle.bottles.__init__`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .. import docker as docker_mod
from .. import network as network_mod
from .. import pipelock
from .. import skills as skills_mod
from .. import ssh as ssh_mod
from ..log import die, info
from ..manifest import Manifest


# --- Runtime detection -----------------------------------------------------


def runsc_available() -> bool:
    """Return True if the Docker daemon has the gVisor (`runsc`) runtime
    registered. Called twice per `start`: once during the preflight to
    render the runtime label, once inside the factory to set `--runtime`.
    `docker info` is cheap; the duplication is not worth caching."""
    r = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and "runsc" in r.stdout


def docker_runtime_label() -> str:
    """Human-readable label for the runtime that `create_docker_bottle`
    would select right now. Shown in the y/N preflight."""
    return "runsc (gVisor)" if runsc_available() else "runc (default)"


# --- Spec ------------------------------------------------------------------


@dataclass(frozen=True)
class DockerBottleSpec:
    """Host-side inputs assembled by the CLI before factory entry. Every
    field is a value the factory consumes; nothing here is platform-
    agnostic enough yet to lift into a shared spec (only Docker exists)."""

    agent_name: str
    slug: str
    manifest: Manifest
    container_name: str
    container_name_pinned: bool
    image: str
    derived_image: str            # "" -> no derived image
    runtime_image: str            # image to actually launch (derived or base)
    user_cwd: str
    copy_cwd_git: bool
    stage_dir: Path
    prompt_file: Path
    env_file: Path
    args_file: Path
    pipelock_yaml_path: Path
    pipelock_yaml_filename: str
    forward_oauth_token: bool


# --- Bottle handle ---------------------------------------------------------


class _DockerBottle:
    """Concrete Bottle for Docker. Holds the resolved container name and
    a teardown closure. Not exported — the factory yields it via the
    Bottle Protocol."""

    def __init__(self, container: str, teardown):
        self.name = container
        self._teardown = teardown
        self._closed = False

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        cmd = ["docker", "exec"]
        if tty:
            cmd.append("-it")
        cmd.extend([self.name, "claude", *argv])
        return subprocess.run(cmd).returncode

    def cp_in(self, host_path: str, container_path: str) -> None:
        subprocess.run(
            ["docker", "cp", host_path, f"{self.name}:{container_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._teardown()


# --- Factory ---------------------------------------------------------------


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)


@contextmanager
def create_docker_bottle(spec: DockerBottleSpec) -> Iterator[_DockerBottle]:
    """Build, launch, and provision a Docker bottle. Teardown on exit."""
    # Teardown bookkeeping. Each entry is populated as the matching
    # resource comes up; teardown walks them in reverse, idempotently.
    state: dict[str, str] = {
        "container": "",
        "pipelock": "",
        "internal_network": "",
        "egress_network": "",
    }

    def teardown() -> None:
        try:
            if state["container"] and docker_mod.container_exists(state["container"]):
                subprocess.run(
                    ["docker", "rm", "-f", state["container"]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                state["container"] = ""
            if state["pipelock"]:
                pipelock.pipelock_stop(spec.slug)
                state["pipelock"] = ""
            if state["internal_network"]:
                network_mod.network_remove(state["internal_network"])
                state["internal_network"] = ""
            if state["egress_network"]:
                network_mod.network_remove(state["egress_network"])
                state["egress_network"] = ""
        except BaseException:
            # Teardown must not raise; swallow so the caller's __exit__
            # path can still propagate the original error.
            pass

    try:
        use_runsc = runsc_available()

        docker_mod.build_image(spec.image, _REPO_DIR)
        if spec.derived_image:
            docker_mod.build_image_with_cwd(spec.derived_image, spec.image, spec.user_cwd)

        state["internal_network"] = network_mod.network_create_internal(spec.slug)
        state["egress_network"] = network_mod.network_create_egress(spec.slug)
        state["pipelock"] = pipelock.pipelock_start(
            spec.slug,
            state["internal_network"],
            state["egress_network"],
            spec.stage_dir,
            spec.pipelock_yaml_filename,
        )

        container = _run_agent_container(spec, state["internal_network"], use_runsc)
        state["container"] = container

        _provision_container(spec, container)

        bottle = _DockerBottle(container, teardown)
        yield bottle
    finally:
        teardown()


# --- Internals -------------------------------------------------------------


def _run_agent_container(
    spec: DockerBottleSpec,
    internal_network: str,
    use_runsc: bool,
) -> str:
    """Build the `docker run` argv and execute it, handling name-conflict
    races by incrementing the suffix (unless the name was user-pinned).
    Returns the resolved container name."""
    proxy_url = pipelock.pipelock_proxy_url(spec.slug)
    docker_args: list[str] = [
        "--rm", "-d",
        "--name", spec.container_name,
        "--network", internal_network,
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", "NO_PROXY=localhost,127.0.0.1",
    ]
    if use_runsc:
        docker_args.extend(["--runtime", "runsc"])
    if spec.env_file.stat().st_size > 0:
        docker_args.extend(["--env-file", str(spec.env_file)])

    # ARGS_FILE pairs (-e, NAME) line-by-line.
    args_lines = spec.args_file.read_text().splitlines()
    i = 0
    while i < len(args_lines):
        flag = args_lines[i]
        i += 1
        if not flag:
            continue
        if i >= len(args_lines):
            break
        vname = args_lines[i]
        i += 1
        docker_args.extend([flag, vname])

    if spec.forward_oauth_token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_BOTTLE_OAUTH_TOKEN"]
        docker_args.extend(["-e", "CLAUDE_CODE_OAUTH_TOKEN"])

    docker_args.extend([spec.runtime_image, "sleep", "infinity"])

    info(f"starting container {spec.container_name} from {spec.runtime_image}")

    container = spec.container_name
    base_name = spec.container_name
    suffix = 2
    while True:
        run_result = subprocess.run(
            ["docker", "run", *docker_args],
            capture_output=True,
            text=True,
        )
        if run_result.returncode == 0:
            return container
        err_text = run_result.stderr
        if spec.container_name_pinned or "is already in use" not in err_text:
            sys.stderr.write(err_text + "\n")
            die(f"docker run failed for container '{container}'")
        if suffix > 100:
            die(
                f"could not find a free container name after "
                f"{base_name}-99 retries; clean up old containers"
            )
        container = f"{base_name}-{suffix}"
        suffix += 1
        name_idx = docker_args.index("--name") + 1
        docker_args[name_idx] = container
        info(f"name conflict; retrying as {container}")


def _provision_container(spec: DockerBottleSpec, container: str) -> None:
    """Copy prompt, skills, ssh keys, and (optionally) .git into the
    running container, fixing up ownership/mode where the host UID
    would otherwise leave files unreadable by the in-container `node`."""
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_prompt_path = f"{container_home}/.claude-bottle-prompt.txt"

    subprocess.run(
        ["docker", "cp", str(spec.prompt_file), f"{container}:{container_prompt_path}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    # `docker cp` preserves host UID; re-own/mode as root so node can
    # read its own mode-600 prompt regardless of host UID.
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chown", "node:node", container_prompt_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chmod", "600", container_prompt_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    agent = spec.manifest.agents[spec.agent_name]
    if agent.skills:
        skills_mod.skills_copy_into(container, list(agent.skills))

    bottle = spec.manifest.bottle_for(spec.agent_name)
    if bottle.ssh:
        proxy_host_port = pipelock.pipelock_proxy_host_port(spec.slug)
        ssh_mod.ssh_setup(container, spec.stage_dir, proxy_host_port, bottle.ssh)

    if spec.copy_cwd_git:
        info(f"copying {spec.user_cwd}/.git -> {container}:/home/node/workspace/.git")
        subprocess.run(
            ["docker", "cp", f"{spec.user_cwd}/.git", f"{container}:/home/node/workspace/.git"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "docker", "exec", "-u", "0", container,
                "chown", "-R", "node:node", "/home/node/workspace/.git",
            ],
            stdout=subprocess.DEVNULL,
            check=True,
        )


def container_prompt_path() -> str:
    """The path inside the container where the prompt file lands. Used
    by start.py to pass `--append-system-prompt-file` to claude."""
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    return f"{container_home}/.claude-bottle-prompt.txt"
