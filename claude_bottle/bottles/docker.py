"""Docker bottle factory.

Two phases:

  prepare_docker_bottle(spec, stage_dir=...) -> DockerBottlePlan
      Resolve names, validate host-side prerequisites, and write
      scratch files (env_file, args_file, prompt, pipelock yaml) to
      stage_dir. No Docker resources are created yet. Suitable to call
      before the y/N preflight.

  launch_docker_bottle(plan) -> ContextManager[Bottle]
      Build the image, create networks, boot the pipelock sidecar,
      launch the agent container (with `--runtime=runsc` iff the
      daemon has gVisor registered), and copy prompt/skills/ssh/.git
      into the running container. Teardown on exit.

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
from ..env_resolve import env_resolve
from ..log import die, info
from ..manifest import Manifest


# --- Runtime detection -----------------------------------------------------


def runsc_available() -> bool:
    """Return True if the Docker daemon has the gVisor (`runsc`) runtime
    registered. Called once per prepare; the result lives on the plan."""
    r = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and "runsc" in r.stdout


# --- Spec + Plan -----------------------------------------------------------


@dataclass(frozen=True)
class BottleSpec:
    """CLI-supplied intent. Platform-agnostic — each platform's prepare
    step consumes it and produces its own platform-specific plan.
    Resolved values (image names, container name, scratch paths, runsc
    availability) live on the plan, not the spec."""

    manifest: Manifest
    agent_name: str
    copy_cwd: bool
    user_cwd: str
    forward_oauth_token: bool


@dataclass(frozen=True)
class DockerBottlePlan:
    """Output of prepare_docker_bottle. Frozen; the launch step consumes
    it without further resolution. show_plan reads from it directly."""

    spec: BottleSpec
    slug: str
    container_name: str
    container_name_pinned: bool
    image: str
    derived_image: str           # "" -> no derived image
    runtime_image: str           # image actually launched (derived or base)
    stage_dir: Path
    env_file: Path
    args_file: Path
    prompt_file: Path
    pipelock_yaml_path: Path
    pipelock_yaml_filename: str
    allowlist_summary: str
    use_runsc: bool


# --- Bottle handle ---------------------------------------------------------


class _DockerBottle:
    """Concrete Bottle for Docker. Holds the container name plus the
    in-container prompt path so exec_claude can transparently add
    --append-system-prompt-file when a prompt was provisioned."""

    def __init__(self, container: str, teardown, prompt_path_in_container: str | None):
        self.name = container
        self._teardown = teardown
        self._prompt_path = prompt_path_in_container
        self._closed = False

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        full_argv = list(argv)
        if self._prompt_path:
            full_argv.extend(["--append-system-prompt-file", self._prompt_path])
        cmd = ["docker", "exec"]
        if tty:
            cmd.append("-it")
        cmd.extend([self.name, "claude", *full_argv])
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


# --- Prepare ---------------------------------------------------------------


def prepare_docker_bottle(spec: BottleSpec, *, stage_dir: Path) -> DockerBottlePlan:
    """Resolve names, validate, write scratch files. No Docker resources
    are created; the only side effects are host-side files under
    stage_dir and a probe of `docker info`."""
    docker_mod.require_docker()

    manifest = spec.manifest
    manifest.require_agent(spec.agent_name)
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)
    bottle_name = agent.bottle

    slug = docker_mod.slugify(spec.agent_name)

    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest")
    derived_image = ""
    runtime_image = image
    if spec.copy_cwd:
        derived_image = os.environ.get(
            "CLAUDE_BOTTLE_DERIVED_IMAGE", f"claude-bottle:cwd-{slug}"
        )
        runtime_image = derived_image

    default_container = f"claude-bottle-{slug}"
    pinned_container = os.environ.get("CLAUDE_BOTTLE_CONTAINER", "")
    container_name = pinned_container or default_container
    container_name_pinned = bool(pinned_container)
    suffix = 2
    if container_name_pinned:
        if docker_mod.container_exists(container_name):
            die(
                f"container '{container_name}' already exists "
                f"(pinned via CLAUDE_BOTTLE_CONTAINER). "
                f"Remove it with 'docker rm -f {container_name}' or unset the override."
            )
    else:
        while docker_mod.container_exists(container_name):
            container_name = f"{default_container}-{suffix}"
            suffix += 1
            if suffix > 100:
                die(
                    f"could not find a free container name after "
                    f"{default_container}-99; clean up old containers with "
                    f"'docker rm -f <name>'"
                )

    if agent.skills:
        skills_mod.skills_validate_all(list(agent.skills))
    if bottle.ssh:
        ssh_mod.ssh_validate_entries(bottle.ssh)

    env_file = stage_dir / "agent.env"
    args_file = stage_dir / "docker-args"
    prompt_file = stage_dir / "prompt.txt"
    pipelock_yaml_filename = "pipelock.yaml"
    pipelock_yaml = stage_dir / pipelock_yaml_filename
    env_file.write_text("")
    env_file.chmod(0o600)
    args_file.write_text("")
    prompt_file.write_text("")
    prompt_file.chmod(0o600)

    pipelock.pipelock_write_yaml(manifest, bottle_name, pipelock_yaml)
    env_resolve(manifest, spec.agent_name, env_file, args_file)
    prompt_file.write_text(agent.prompt)

    allowlist_summary = pipelock.pipelock_allowlist_summary(manifest, bottle_name)
    use_runsc = runsc_available()

    return DockerBottlePlan(
        spec=spec,
        slug=slug,
        container_name=container_name,
        container_name_pinned=container_name_pinned,
        image=image,
        derived_image=derived_image,
        runtime_image=runtime_image,
        stage_dir=stage_dir,
        env_file=env_file,
        args_file=args_file,
        prompt_file=prompt_file,
        pipelock_yaml_path=pipelock_yaml,
        pipelock_yaml_filename=pipelock_yaml_filename,
        allowlist_summary=allowlist_summary,
        use_runsc=use_runsc,
    )


# --- Launch ----------------------------------------------------------------


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)


@contextmanager
def launch_docker_bottle(plan: DockerBottlePlan) -> Iterator[_DockerBottle]:
    """Build, launch, and provision a Docker bottle. Teardown on exit."""
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
                pipelock.pipelock_stop(plan.slug)
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
        docker_mod.build_image(plan.image, _REPO_DIR)
        if plan.derived_image:
            docker_mod.build_image_with_cwd(
                plan.derived_image, plan.image, plan.spec.user_cwd
            )

        state["internal_network"] = network_mod.network_create_internal(plan.slug)
        state["egress_network"] = network_mod.network_create_egress(plan.slug)
        state["pipelock"] = pipelock.pipelock_start(
            plan.slug,
            state["internal_network"],
            state["egress_network"],
            plan.stage_dir,
            plan.pipelock_yaml_filename,
        )

        container = _run_agent_container(plan, state["internal_network"])
        state["container"] = container

        prompt_path = _provision_container(plan, container)

        bottle = _DockerBottle(container, teardown, prompt_path)
        yield bottle
    finally:
        teardown()


# --- Internals -------------------------------------------------------------


def _run_agent_container(plan: DockerBottlePlan, internal_network: str) -> str:
    """Build the `docker run` argv and execute it, handling name-conflict
    races by incrementing the suffix (unless the name was user-pinned).
    Returns the resolved container name."""
    proxy_url = pipelock.pipelock_proxy_url(plan.slug)
    docker_args: list[str] = [
        "--rm", "-d",
        "--name", plan.container_name,
        "--network", internal_network,
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", "NO_PROXY=localhost,127.0.0.1",
    ]
    if plan.use_runsc:
        docker_args.extend(["--runtime", "runsc"])
    if plan.env_file.stat().st_size > 0:
        docker_args.extend(["--env-file", str(plan.env_file)])

    # ARGS_FILE pairs (-e, NAME) line-by-line.
    args_lines = plan.args_file.read_text().splitlines()
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

    if plan.spec.forward_oauth_token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_BOTTLE_OAUTH_TOKEN"]
        docker_args.extend(["-e", "CLAUDE_CODE_OAUTH_TOKEN"])

    docker_args.extend([plan.runtime_image, "sleep", "infinity"])

    info(f"starting container {plan.container_name} from {plan.runtime_image}")

    container = plan.container_name
    base_name = plan.container_name
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
        if plan.container_name_pinned or "is already in use" not in err_text:
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


def _provision_container(plan: DockerBottlePlan, container: str) -> str | None:
    """Copy prompt, skills, ssh keys, and (optionally) .git into the
    running container. Returns the in-container prompt path if a prompt
    was provisioned, else None — the Bottle handle uses it to decide
    whether to add --append-system-prompt-file to claude's argv."""
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    in_container_prompt_path = f"{container_home}/.claude-bottle-prompt.txt"

    subprocess.run(
        ["docker", "cp", str(plan.prompt_file), f"{container}:{in_container_prompt_path}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    # `docker cp` preserves host UID; re-own/mode as root so node can
    # read its own mode-600 prompt regardless of host UID.
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chown", "node:node", in_container_prompt_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chmod", "600", in_container_prompt_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    if agent.skills:
        skills_mod.skills_copy_into(container, list(agent.skills))

    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if bottle.ssh:
        proxy_host_port = pipelock.pipelock_proxy_host_port(plan.slug)
        ssh_mod.ssh_setup(container, plan.stage_dir, proxy_host_port, bottle.ssh)

    if plan.spec.copy_cwd and Path(plan.spec.user_cwd, ".git").is_dir():
        info(f"copying {plan.spec.user_cwd}/.git -> {container}:/home/node/workspace/.git")
        subprocess.run(
            ["docker", "cp", f"{plan.spec.user_cwd}/.git", f"{container}:/home/node/workspace/.git"],
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

    return in_container_prompt_path if agent.prompt else None
