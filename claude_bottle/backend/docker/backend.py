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
from typing import Iterator, Sequence

from ... import pipelock
from ...env_resolve import env_resolve
from ...log import die, info
from ...manifest import SshEntry
from ...util import expand_tilde
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
            self.validate_skills(list(agent.skills))
        if bottle.ssh:
            self.validate_ssh_entries(bottle.ssh)

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

        self.prepare_proxy(spec, pipelock_yaml)
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

    def prepare_proxy(self, spec: BottleSpec, yaml_path: Path) -> None:
        """Write the pipelock proxy's yaml config (mode 600) to
        `yaml_path` for the sidecar to consume when it boots in
        `launch`. Carries the effective allowlist (bottle.egress.allowlist
        UNION claude-bottle defaults UNION ssh hostnames), a fixed
        listen port, strict mode + forward_proxy + DLP defaults +
        scan_env. Deliberately contains no env values, no secrets, no
        per-agent customization beyond the hostname list."""
        bottle_name = spec.manifest.agents[spec.agent_name].bottle
        allowlist = pipelock.pipelock_effective_allowlist(spec.manifest, bottle_name)
        trusted = pipelock.pipelock_bottle_ssh_trusted_domains(spec.manifest, bottle_name)
        ip_cidrs = pipelock.pipelock_bottle_ssh_ip_cidrs(spec.manifest, bottle_name)

        lines: list[str] = []
        lines.append("version: 1")
        lines.append("mode: strict")
        lines.append("enforce: true")
        lines.append("")
        lines.append("# Hostnames the agent is allowed to reach. Effective list is")
        lines.append("# claude-bottle defaults UNION bottle.egress.allowlist (sorted, deduped).")
        lines.append("api_allowlist:")
        for h in allowlist:
            lines.append(f'  - "{h}"')
        lines.append("")
        lines.append("forward_proxy:")
        lines.append("  enabled: true")
        lines.append("")
        if trusted:
            lines.append("trusted_domains:")
            for td in trusted:
                lines.append(f'  - "{td}"')
            lines.append("")
        if ip_cidrs:
            lines.append("ssrf:")
            lines.append("  ip_allowlist:")
            for cidr in ip_cidrs:
                lines.append(f'    - "{cidr}"')
            lines.append("")
        lines.append("dlp:")
        lines.append("  include_defaults: true")
        lines.append("  scan_env: true")

        yaml_path.write_text("\n".join(lines) + "\n")
        yaml_path.chmod(0o600)

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

    def provision_prompt(self, plan: BottlePlan, target: str) -> str | None:
        """Copy the prompt file into the container, fix ownership/mode.
        Returns the in-container path if the agent has a non-empty
        prompt (drives --append-system-prompt-file), else None. The
        file is copied either way so the path always exists."""
        assert isinstance(plan, DockerBottlePlan)
        container = target
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

    def validate_skills(self, skills: list[str]) -> None:
        """Fail loudly if any named skill is missing from the host's
        ~/.claude/skills/. Called from `prepare` before the y/N so the
        user doesn't get a launch prompt for a plan that's already
        known to break."""
        for name in skills:
            path = self._host_skill_dir(name)
            if not os.path.isdir(path):
                die(
                    f"skill '{name}' not found on host at {path}. "
                    f"Create it under ~/.claude/skills/, then re-run."
                )

    def _host_skill_dir(self, name: str) -> str:
        home = os.environ.get("HOME")
        if not home:
            die("HOME not set")
        return f"{home}/.claude/skills/{name}"

    def provision_skills(self, plan: BottlePlan, target: str) -> None:
        """Copy each of the agent's named skills from the host's
        ~/.claude/skills/<name>/ into the container's equivalent path.
        For each skill: ensure parent dir, wipe any prior copy, then
        `docker cp <host>/. <container>:<dst>/` so the contents are
        copied into a freshly-created destination dir. No-op when the
        agent has no skills."""
        assert isinstance(plan, DockerBottlePlan)
        agent = plan.spec.manifest.agents[plan.spec.agent_name]
        if not agent.skills:
            return

        container = target
        container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
        skills_dir = os.environ.get(
            "CLAUDE_BOTTLE_CONTAINER_SKILLS_DIR", f"{container_home}/.claude/skills"
        )

        subprocess.run(
            ["docker", "exec", container, "mkdir", "-p", skills_dir],
            stdout=subprocess.DEVNULL,
            check=True,
        )

        for n in agent.skills:
            src = self._host_skill_dir(n)
            if not os.path.isdir(src):
                die(f"skill '{n}' disappeared from host between validation and copy at {src}.")
            dst = f"{skills_dir}/{n}"
            info(f"copying skill {n} into {container}:{dst}")
            subprocess.run(
                ["docker", "exec", container, "rm", "-rf", dst],
                stdout=subprocess.DEVNULL,
                check=True,
            )
            subprocess.run(
                ["docker", "exec", container, "mkdir", "-p", dst],
                stdout=subprocess.DEVNULL,
                check=True,
            )
            subprocess.run(
                ["docker", "cp", f"{src}/.", f"{container}:{dst}/"],
                stdout=subprocess.DEVNULL,
                check=True,
            )

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

    def provision_ssh(self, plan: BottlePlan, target: str) -> None:
        """Set up SSH in the container so node can authenticate using
        each entry's key without the key file being readable by node.
        No-op when the bottle has no SSH entries.

        Isolation strategy:
          - Keys live at /root/.claude-bottle-keys/ (mode 700,
            root-owned). /root is mode 700 in node:22-slim, so node
            (uid 1000) can't even traverse in.
          - ssh-agent runs as root, listening on
            /run/claude-bottle-agent.sock. Each key is loaded with
            ssh-add, then deleted; the bytes now live only in the
            agent process's memory.
          - ssh-agent's SO_PEERCRED-based UID match rejects every
            connection whose peer euid is neither 0 nor the agent's.
            To bridge that, a root-owned socat forwarder listens on
            /run/claude-bottle-agent-public.sock (mode 666) and
            proxies bytes to the real agent socket.
          - node can't ptrace root-owned agent or socat, so
            /proc/<pid>/mem is off-limits and key bytes never leave
            root-owned memory.
          - ~/.ssh/config in node's home points each Host at the
            public socket via IdentityAgent.

        Why an in-container agent (not bind-mounted from host):
        Docker Desktop on macOS does not forward Unix-domain socket
        connect() across the VM boundary — connect() returns
        ENOTSUP. Running ssh-agent inside the container sidesteps
        that entirely.

        Limitation: keys must be passphrase-less. ssh-add prompts on
        /dev/tty for passphrases, but our docker exec has no TTY."""
        assert isinstance(plan, DockerBottlePlan)
        bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
        if not bottle.ssh:
            return

        container = target
        proxy_host_port = pipelock.pipelock_proxy_host_port(plan.slug)
        container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
        container_ssh = f"{container_home}/.ssh"
        agent_socket = "/run/claude-bottle-agent.sock"
        public_socket = "/run/claude-bottle-agent-public.sock"
        keys_dir = "/root/.claude-bottle-keys"

        # ~/.ssh for node (700, owned by node).
        self._docker_exec_root(container, ["mkdir", "-p", container_ssh])
        self._docker_exec_root(container, ["chown", "node:node", container_ssh])
        self._docker_exec_root(container, ["chmod", "700", container_ssh])

        # /root/.claude-bottle-keys for root (700, root-owned).
        self._docker_exec_root(container, ["mkdir", "-p", keys_dir])
        self._docker_exec_root(container, ["chown", "root:root", keys_dir])
        self._docker_exec_root(container, ["chmod", "700", keys_dir])

        config_file = plan.stage_dir / "ssh_config"
        known_hosts_file = plan.stage_dir / "ssh_known_hosts"
        config_file.write_text("")
        config_file.chmod(0o600)
        known_hosts_file.write_text("")
        known_hosts_file.chmod(0o600)

        proxy_host, _, proxy_port = proxy_host_port.partition(":")

        container_key_paths: list[str] = []
        for entry in bottle.ssh:
            name = entry.Host
            key = expand_tilde(entry.IdentityFile)
            hostname = entry.Hostname
            user = entry.User
            port = entry.Port
            known_host_key = entry.KnownHostKey

            key_basename = os.path.basename(key)
            container_key_path = f"{keys_dir}/{key_basename}"

            info(f"copying ssh key for '{name}' -> {container} (root-only staging)")
            subprocess.run(
                ["docker", "cp", key, f"{container}:{container_key_path}"],
                stdout=subprocess.DEVNULL,
                check=True,
            )
            self._docker_exec_root(container, ["chown", "root:root", container_key_path])
            self._docker_exec_root(container, ["chmod", "600", container_key_path])

            container_key_paths.append(container_key_path)

            # ProxyCommand tunnels SSH through pipelock via HTTP
            # CONNECT. %h / %p expand to this block's HostName /
            # Port. socat's PROXY: mode does CONNECT host:port to
            # the proxy.
            block = (
                f"Host {name}\n"
                f"  HostName {hostname}\n"
                f"  User {user}\n"
                f"  Port {port}\n"
                f"  IdentityAgent {public_socket}\n"
                f"  ProxyCommand socat - PROXY:{proxy_host}:%h:%p,proxyport={proxy_port}\n"
                f"\n"
            )
            with config_file.open("a") as f:
                f.write(block)

            if known_host_key:
                entries_to_write: list[str] = []
                if port == "22":
                    entries_to_write.append(f"{name} {known_host_key}\n")
                    if hostname != name:
                        entries_to_write.append(f"{hostname} {known_host_key}\n")
                else:
                    entries_to_write.append(f"[{name}]:{port} {known_host_key}\n")
                    if hostname != name:
                        entries_to_write.append(f"[{hostname}]:{port} {known_host_key}\n")
                with known_hosts_file.open("a") as f:
                    for e in entries_to_write:
                        f.write(e)

        # Boot the agent, load each key, delete the key files, then
        # start the root-owned socat forwarder. One docker exec so the
        # whole sequence is atomic.
        info(f"starting in-container ssh-agent at {agent_socket} (forwarded via {public_socket})")
        setup_lines = [
            "set -eu",
            f"ssh-agent -a {agent_socket} >/dev/null",
        ]
        for kp in container_key_paths:
            setup_lines.append(f"SSH_AUTH_SOCK={agent_socket} ssh-add {kp}")
            setup_lines.append(f"rm -f {kp}")
        setup_lines.append(f"rmdir {keys_dir} 2>/dev/null || true")
        # Forwarder: socat (uid 0) connects to the agent on node's behalf.
        setup_lines.append(
            f"nohup socat UNIX-LISTEN:{public_socket},fork,reuseaddr,mode=666 "
            f"UNIX-CONNECT:{agent_socket} </dev/null >/dev/null 2>&1 &"
        )
        # Wait briefly for the forwarder to bind.
        setup_lines.extend([
            "i=0",
            "while [ $i -lt 20 ]; do",
            f"  [ -S {public_socket} ] && break",
            "  i=$((i + 1))",
            "  sleep 0.1",
            "done",
            f"[ -S {public_socket} ] || {{ echo 'claude-bottle: socat forwarder failed to bind {public_socket}' >&2; exit 1; }}",
        ])
        setup_script = "\n".join(setup_lines) + "\n"
        subprocess.run(
            ["docker", "exec", "-u", "0", container, "sh", "-c", setup_script],
            check=True,
        )

        info(f"writing {container_ssh}/config")
        subprocess.run(
            ["docker", "cp", str(config_file), f"{container}:{container_ssh}/config"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        self._docker_exec_root(container, ["chown", "node:node", f"{container_ssh}/config"])
        self._docker_exec_root(container, ["chmod", "600", f"{container_ssh}/config"])

        if known_hosts_file.stat().st_size > 0:
            info(f"writing {container_ssh}/known_hosts")
            subprocess.run(
                ["docker", "cp", str(known_hosts_file), f"{container}:{container_ssh}/known_hosts"],
                stdout=subprocess.DEVNULL,
                check=True,
            )
            self._docker_exec_root(container, ["chown", "node:node", f"{container_ssh}/known_hosts"])
            self._docker_exec_root(container, ["chmod", "600", f"{container_ssh}/known_hosts"])

    def _docker_exec_root(self, container: str, argv: list[str]) -> None:
        subprocess.run(
            ["docker", "exec", "-u", "0", container, *argv],
            stdout=subprocess.DEVNULL,
            check=True,
        )

    def provision_git(self, plan: BottlePlan, target: str) -> None:
        """If --cwd was set and the host cwd has a .git directory, copy
        it into /home/node/workspace/.git and fix ownership. No-op
        otherwise."""
        assert isinstance(plan, DockerBottlePlan)
        if not (plan.spec.copy_cwd and Path(plan.spec.user_cwd, ".git").is_dir()):
            return
        container = target
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
