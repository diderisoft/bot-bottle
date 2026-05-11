"""DockerBottleBackend — the Docker implementation of BottleBackend.

Methods:
  .prepare(spec, stage_dir=...) -> DockerBottlePlan
  .launch(plan) -> ContextManager[DockerBottle]
  .prepare_cleanup() -> DockerBottleCleanupPlan
  .cleanup(plan) -> None
  .list_active() -> None
"""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ... import pipelock
from ... import skills as skills_mod
from ... import ssh as ssh_mod
from ...env_resolve import env_resolve
from ...log import die, info
from .. import BottleBackend, BottleCleanupPlan, BottlePlan, BottleSpec
from . import network as network_mod
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


class DockerBottleBackend(BottleBackend):
    """Docker backend implementation. Selected by CLAUDE_BOTTLE_BACKEND
    (default)."""

    name = "docker"

    def prepare(self, spec: BottleSpec, *, stage_dir: Path) -> DockerBottlePlan:
        """Resolve names, validate, write scratch files. No Docker
        resources are created; the only side effects are host-side
        files under stage_dir and a probe of `docker info`."""
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
        use_runsc = docker_mod.runsc_available()

        return DockerBottlePlan(
            spec=spec,
            stage_dir=stage_dir,
            slug=slug,
            container_name=container_name,
            container_name_pinned=container_name_pinned,
            image=image,
            derived_image=derived_image,
            runtime_image=runtime_image,
            env_file=env_file,
            args_file=args_file,
            prompt_file=prompt_file,
            pipelock_yaml_path=pipelock_yaml,
            pipelock_yaml_filename=pipelock_yaml_filename,
            allowlist_summary=allowlist_summary,
            use_runsc=use_runsc,
        )

    @contextmanager
    def launch(self, plan: BottlePlan) -> Iterator[DockerBottle]:
        """Build, launch, and provision a Docker bottle. Teardown on exit."""
        assert isinstance(plan, DockerBottlePlan), (
            f"DockerBottleBackend.launch expects DockerBottlePlan, "
            f"got {type(plan).__name__}"
        )

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
                # Teardown must not raise; swallow so the caller's
                # __exit__ path can still propagate the original error.
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

            container = self._run_agent_container(plan, state["internal_network"])
            state["container"] = container

            prompt_path = self.provision(plan, container)

            bottle = DockerBottle(container, teardown, prompt_path)
            yield bottle
        finally:
            teardown()

    def _run_agent_container(self, plan: DockerBottlePlan, internal_network: str) -> str:
        """Build the `docker run` argv and execute it, handling
        name-conflict races by incrementing the suffix (unless the name
        was user-pinned). Returns the resolved container name."""
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

    def provision(self, plan: BottlePlan, target: str) -> str | None:
        """Copy prompt, skills, ssh keys, and (optionally) .git into
        the running container. `target` is the resolved container
        name. Returns the in-container prompt path if a prompt was
        provisioned, else None — the Bottle handle uses it to decide
        whether to add --append-system-prompt-file to claude's argv."""
        assert isinstance(plan, DockerBottlePlan), (
            f"DockerBottleBackend.provision expects DockerBottlePlan, "
            f"got {type(plan).__name__}"
        )
        container = target

        prompt_path = self.provision_prompt(plan, container)
        agent = plan.spec.manifest.agents[plan.spec.agent_name]
        if agent.skills:
            skills_mod.skills_copy_into(container, list(agent.skills))
        self.provision_ssh(plan, container)
        self.provision_git(plan, container)
        return prompt_path

    def provision_prompt(self, plan: DockerBottlePlan, container: str) -> str | None:
        """Copy the prompt file into the container, fix ownership/mode.
        Returns the in-container path if the agent has a non-empty
        prompt (drives --append-system-prompt-file), else None. The
        file is copied either way so the path always exists."""
        container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
        in_container_prompt_path = f"{container_home}/.claude-bottle-prompt.txt"

        subprocess.run(
            ["docker", "cp", str(plan.prompt_file), f"{container}:{in_container_prompt_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        # `docker cp` preserves host UID; re-own/mode as root so node
        # can read its own mode-600 prompt regardless of host UID.
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
        return in_container_prompt_path if agent.prompt else None

    def provision_ssh(self, plan: DockerBottlePlan, container: str) -> None:
        """If the bottle has SSH entries, set up the in-container
        ssh-agent and config so node can authenticate without ever
        seeing the key bytes. No-op when the bottle has no SSH."""
        bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
        if not bottle.ssh:
            return
        proxy_host_port = pipelock.pipelock_proxy_host_port(plan.slug)
        ssh_mod.ssh_setup(container, plan.stage_dir, proxy_host_port, bottle.ssh)

    def provision_git(self, plan: DockerBottlePlan, container: str) -> None:
        """If --cwd was set and the host cwd has a .git directory, copy
        it into /home/node/workspace/.git and fix ownership. No-op
        otherwise."""
        if not (plan.spec.copy_cwd and Path(plan.spec.user_cwd, ".git").is_dir()):
            return
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

    # --- Cleanup ---

    def prepare_cleanup(self) -> DockerBottleCleanupPlan:
        """Enumerate all claude-bottle-prefixed containers (running or
        stopped) and networks. No removals — caller confirms first."""
        docker_mod.require_docker()

        # `docker ps -a --filter name=...` uses regex matching; anchor at
        # the start so we don't pick up containers that merely contain
        # "claude-bottle-" mid-name.
        cr = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "name=^claude-bottle-",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
        )
        containers = tuple(sorted(
            line for line in (cr.stdout or "").splitlines() if line
        ))

        # `docker network ls --filter name=...` uses substring matching.
        # "claude-bottle-" is specific enough that false positives are
        # not a concern.
        nr = subprocess.run(
            [
                "docker", "network", "ls",
                "--filter", "name=claude-bottle-",
                "--format", "{{.Name}}",
            ],
            capture_output=True,
            text=True,
        )
        networks = tuple(sorted(
            line for line in (nr.stdout or "").splitlines() if line
        ))

        return DockerBottleCleanupPlan(containers=containers, networks=networks)

    def cleanup(self, plan: BottleCleanupPlan) -> None:
        """Remove the containers and networks listed in the plan.
        Containers first; networks would refuse to delete while
        containers are still attached."""
        assert isinstance(plan, DockerBottleCleanupPlan), (
            f"DockerBottleBackend.cleanup expects DockerBottleCleanupPlan, "
            f"got {type(plan).__name__}"
        )
        for name in plan.containers:
            info(f"removing container {name}")
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for name in plan.networks:
            info(f"removing network {name}")
            subprocess.run(
                ["docker", "network", "rm", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    # --- List ---

    def list_active(self) -> None:
        """Print all running claude-bottle containers (name + status).
        Prints a single-line banner if there are none."""
        docker_mod.require_docker()
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "name=^claude-bottle-",
                "--format", "{{.Names}}\t{{.Status}}",
            ],
            capture_output=True,
            text=True,
        )
        containers = (result.stdout or "").strip()
        if not containers:
            info("no active claude-bottle containers")
            return
        print()
        for line in containers.splitlines():
            name, _, status = line.partition("\t")
            info(f"container: {name}  status: {status}")
        print()
