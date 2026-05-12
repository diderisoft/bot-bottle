"""DockerBottleBackend — the Docker implementation of BottleBackend.

Methods:
  .prepare(spec, stage_dir=...) -> DockerBottlePlan
  .launch(plan) -> ContextManager[DockerBottle]
  .prepare_cleanup() -> DockerBottleCleanupPlan
  .cleanup(plan) -> None
  .list_active() -> None
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from ... import pipelock
from ...env import ResolvedEnv, resolve_env
from ...log import die, info
from ...manifest import SshEntry
from ...util import expand_tilde
from .. import BottleBackend, BottleSpec
from ..util import host_skill_dir
from . import network as network_mod
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan
from .pipelock import (
    DockerPipelockProxy,
    pipelock_proxy_url,
)
from .provision import git as _git
from .provision import prompt as _prompt
from .provision import skills as _skills
from .provision import ssh as _ssh


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def _force_remove_container(name: str) -> None:
    if docker_mod.container_exists(name):
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class DockerBottleBackend(BottleBackend["DockerBottlePlan", "DockerBottleCleanupPlan"]):
    """Docker backend implementation. Selected by CLAUDE_BOTTLE_BACKEND
    (default)."""

    name = "docker"
    _proxy: DockerPipelockProxy = DockerPipelockProxy()

    def prepare(self, spec: BottleSpec, *, stage_dir: Path) -> DockerBottlePlan:
        """Resolve names, validate, write scratch files. No Docker
        resources are created; the only side effects are host-side
        files under stage_dir and a probe of `docker info`."""
        docker_mod.require_docker()

        manifest = spec.manifest
        manifest.require_agent(spec.agent_name)
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)

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
            self.validate_skills(list(agent.skills))
        if bottle.ssh:
            self.validate_ssh_entries(bottle.ssh)

        env_file = stage_dir / "agent.env"
        args_file = stage_dir / "docker-args"
        prompt_file = stage_dir / "prompt.txt"
        prompt_file.write_text("")
        prompt_file.chmod(0o600)

        proxy_plan = self._proxy.prepare(bottle, slug, stage_dir)
        resolved = resolve_env(manifest, spec.agent_name)
        self._write_env_files(resolved, env_file, args_file)
        prompt_file.write_text(agent.prompt)

        allowlist_summary = pipelock.pipelock_allowlist_summary(bottle)
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
            proxy_plan=proxy_plan,
            allowlist_summary=allowlist_summary,
            use_runsc=use_runsc,
        )

    def _write_env_files(
        self, resolved: ResolvedEnv, env_file: Path, args_file: Path
    ) -> None:
        """Serialize a ResolvedEnv into the two on-disk formats the launch
        step consumes: `--env-file` syntax for literals (NAME=VALUE per
        line) and a paired `-e\\nNAME\\n` stream for forwarded names.
        Both files are created here (mode 600 on the literals file,
        which may carry sensitive verbatim values from the manifest)."""
        env_lines: list[str] = []
        for name, value in resolved.literals.items():
            if "\n" in value:
                die(
                    f"env entry {name} (literal) contains a newline; "
                    f"docker --env-file cannot represent multi-line values."
                )
            env_lines.append(f"{name}={value}")
        env_file.write_text("\n".join(env_lines) + ("\n" if env_lines else ""))
        env_file.chmod(0o600)

        args_lines = [f"-e\n{name}" for name in resolved.forwarded]
        args_file.write_text("\n".join(args_lines) + ("\n" if args_lines else ""))

    @contextmanager
    def launch(self, plan: DockerBottlePlan) -> Iterator[DockerBottle]:
        """Build, launch, and provision a Docker bottle. Teardown on exit."""
        stack = ExitStack()

        def teardown() -> None:
            try:
                stack.close()
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

            internal_network = network_mod.network_create_internal(plan.slug)
            stack.callback(network_mod.network_remove, internal_network)

            egress_network = network_mod.network_create_egress(plan.slug)
            stack.callback(network_mod.network_remove, egress_network)

            proxy_plan = dataclasses.replace(
                plan.proxy_plan,
                internal_network=internal_network,
                egress_network=egress_network,
            )
            pipelock_name = self._proxy.start(proxy_plan)
            stack.callback(self._proxy.stop, pipelock_name)

            container = self._run_agent_container(plan, internal_network)
            stack.callback(_force_remove_container, container)

            prompt_path = self.provision(plan, container)

            yield DockerBottle(container, teardown, prompt_path)
        finally:
            teardown()

    def _run_agent_container(self, plan: DockerBottlePlan, internal_network: str) -> str:
        """Build the `docker run` argv and execute it, handling
        name-conflict races by incrementing the suffix (unless the name
        was user-pinned). Returns the resolved container name."""
        proxy_url = pipelock_proxy_url(plan.slug)
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

    def provision_prompt(self, plan: DockerBottlePlan, target: str) -> str | None:
        return _prompt.provision_prompt(plan, target)

    def validate_skills(self, skills: list[str]) -> None:
        """Fail loudly if any named skill is missing from the host's
        ~/.claude/skills/. Called from `prepare` before the y/N so the
        user doesn't get a launch prompt for a plan that's already
        known to break."""
        for name in skills:
            path = host_skill_dir(name)
            if not os.path.isdir(path):
                die(
                    f"skill '{name}' not found on host at {path}. "
                    f"Create it under ~/.claude/skills/, then re-run."
                )

    def provision_skills(self, plan: DockerBottlePlan, target: str) -> None:
        _skills.provision_skills(plan, target)

    def validate_ssh_entries(self, entries: Sequence[SshEntry]) -> None:
        """Each entry's IdentityFile must exist on the host (after
        expanding leading ~). Host and IdentityFile shape are already
        enforced by Manifest validation. Called from `prepare` before
        the y/N so the user doesn't get prompted for a plan with a
        missing key."""
        for entry in entries:
            key = expand_tilde(entry.IdentityFile)
            if not os.path.isfile(key):
                die(f"ssh key file not found for host '{entry.Host}': {key}")

    def provision_ssh(self, plan: DockerBottlePlan, target: str) -> None:
        _ssh.provision_ssh(plan, target)

    def provision_git(self, plan: DockerBottlePlan, target: str) -> None:
        _git.provision_git(plan, target)

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

    def cleanup(self, plan: DockerBottleCleanupPlan) -> None:
        """Remove the containers and networks listed in the plan.
        Containers first; networks would refuse to delete while
        containers are still attached."""
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
