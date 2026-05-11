"""start: boot a sandboxed container for a named agent and attach an
interactive claude-code session. The container is torn down when the
session ends."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .. import docker as docker_mod
from .. import pipelock
from .. import skills as skills_mod
from .. import ssh as ssh_mod
from ..bottles import get_bottle_factory
from ..bottles.docker import (
    DockerBottleSpec,
    container_prompt_path,
    docker_runtime_label,
)
from ..env_resolve import env_resolve
from ..log import die, info
from ..manifest import Manifest
from ._common import PROG, USER_CWD, read_tty_line


def show_plan(spec: DockerBottleSpec, *, remote_control: bool) -> None:
    """Render the y/N preflight summary to stderr. Pure presentation;
    reads manifest-backed fields off `spec` and probes the Docker
    runtime label. `remote_control` is the only field not already on
    the spec — it's a claude CLI flag, not a bottle property."""
    manifest = spec.manifest
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)

    env_names = list(bottle.env.keys())
    if spec.forward_oauth_token:
        env_names.append("CLAUDE_CODE_OAUTH_TOKEN")

    ssh_hosts = [e.Host for e in bottle.ssh]
    allowlist_summary = pipelock.pipelock_allowlist_summary(manifest, agent.bottle)
    prompt_first_line = agent.prompt.splitlines()[0] if agent.prompt else ""

    print(file=sys.stderr)
    info(f"agent           : {spec.agent_name}")
    info(f"image           : {spec.image}")
    if spec.derived_image:
        info(
            f"cwd             : {spec.user_cwd} -> /home/node/workspace "
            f"(derived: {spec.derived_image})"
        )
    info(f"container       : {spec.container_name}")
    info(f"stage dir       : {spec.stage_dir}")
    info("env (names only): " + (", ".join(env_names) if env_names else "(none)"))
    info("skills          : " + (" ".join(agent.skills) if agent.skills else "(none)"))
    info(f"docker runtime  : {docker_runtime_label()}")
    info(f"bottle             : {agent.bottle}")
    if ssh_hosts:
        info(f"  ssh hosts     : {', '.join(ssh_hosts)}")
    else:
        info("  ssh hosts     : (none)")
    info(f"  egress        : {allowlist_summary}")
    info(
        f"prompt          : {len(agent.prompt)} chars; "
        f"first line: {prompt_first_line or '(empty)'}"
    )
    info("remote-control  : " + ("enabled" if remote_control else "disabled"))
    print(file=sys.stderr)


def cmd_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cwd", action="store_true", help="copy host cwd into a derived image")
    parser.add_argument("--remote-control", action="store_true")
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get("CLAUDE_BOTTLE_DRY_RUN") == "1"

    name = args.name
    slug = docker_mod.slugify(name)

    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest")
    default_container = f"claude-bottle-{slug}"
    pinned_container = os.environ.get("CLAUDE_BOTTLE_CONTAINER", "")

    runtime_image = image
    derived_image = ""
    if args.cwd:
        derived_image = os.environ.get("CLAUDE_BOTTLE_DERIVED_IMAGE", f"claude-bottle:cwd-{slug}")
        runtime_image = derived_image

    docker_mod.require_docker()
    manifest = Manifest.resolve(USER_CWD)
    manifest.require_agent(name)
    agent = manifest.agents[name]
    bottle_name = agent.bottle
    bottle = manifest.bottle_for(name)

    container = pinned_container or default_container
    suffix = 2
    if pinned_container:
        if docker_mod.container_exists(container):
            die(
                f"container '{container}' already exists "
                f"(pinned via CLAUDE_BOTTLE_CONTAINER). "
                f"Remove it with 'docker rm -f {container}' or unset the override."
            )
    else:
        while docker_mod.container_exists(container):
            container = f"{default_container}-{suffix}"
            suffix += 1
            if suffix > 100:
                die(
                    f"could not find a free container name after "
                    f"{default_container}-99; clean up old containers with "
                    f"'docker rm -f <name>'"
                )

    # CLAUDE_BOTTLE_OAUTH_TOKEN → CLAUDE_CODE_OAUTH_TOKEN forwarding.
    # Host-side token is always forwarded so every container can authenticate.
    forward_oauth_token = bool(os.environ.get("CLAUDE_BOTTLE_OAUTH_TOKEN"))

    if agent.skills:
        skills_mod.skills_validate_all(list(agent.skills))

    if bottle.ssh:
        ssh_mod.ssh_validate_entries(bottle.ssh)

    stage_dir = Path(tempfile.mkdtemp(prefix="claude-bottle-stage."))
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

    try:
        pipelock.pipelock_write_yaml(manifest, bottle_name, pipelock_yaml)

        env_resolve(manifest, name, env_file, args_file)

        prompt_content = agent.prompt
        prompt_file.write_text(prompt_content)

        spec = DockerBottleSpec(
            agent_name=name,
            slug=slug,
            manifest=manifest,
            container_name=container,
            container_name_pinned=bool(pinned_container),
            image=image,
            derived_image=derived_image,
            runtime_image=runtime_image,
            user_cwd=USER_CWD,
            copy_cwd_git=bool(args.cwd and Path(USER_CWD, ".git").is_dir()),
            stage_dir=stage_dir,
            prompt_file=prompt_file,
            env_file=env_file,
            args_file=args_file,
            pipelock_yaml_path=pipelock_yaml,
            pipelock_yaml_filename=pipelock_yaml_filename,
            forward_oauth_token=forward_oauth_token,
        )

        show_plan(spec, remote_control=args.remote_control)

        if dry_run:
            info("dry-run requested; not starting container.")
            return 0

        sys.stderr.write("claude-bottle: launch this agent? [y/N] ")
        sys.stderr.flush()
        reply = read_tty_line()
        if reply not in ("y", "Y", "yes", "YES"):
            info("aborted by user")
            return 0

        factory = get_bottle_factory()
        with factory(spec) as bottle_handle:
            info(
                "attaching interactive claude session "
                "(Ctrl-D or 'exit' to leave; container will be removed)"
            )
            claude_args = ["--dangerously-skip-permissions"]
            if args.remote_control:
                claude_args.append("--remote-control")
            if prompt_content:
                claude_args.extend(
                    ["--append-system-prompt-file", container_prompt_path()]
                )
            bottle_handle.exec_claude(claude_args, tty=True)
            info(f"session ended; container {bottle_handle.name} will be removed")
            return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
