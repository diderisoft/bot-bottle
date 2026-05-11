"""start: boot a sandboxed container for a named agent and attach an
interactive claude-code session. The container is torn down when the
session ends."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import docker as docker_mod
from .. import network as network_mod
from .. import pipelock
from .. import skills as skills_mod
from .. import ssh as ssh_mod
from ..env_resolve import env_resolve
from ..log import die, info
from ..manifest import Manifest
from ._common import PROG, REPO_DIR, USER_CWD, read_tty_line


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

    # --- Plan resolution (host-only, no container yet) ---
    env_names = list(bottle.env.keys())

    # CLAUDE_BOTTLE_OAUTH_TOKEN → CLAUDE_CODE_OAUTH_TOKEN forwarding.
    # Host-side token is always forwarded so every container can authenticate.
    forward_oauth_token = bool(os.environ.get("CLAUDE_BOTTLE_OAUTH_TOKEN"))
    display_env_names = list(env_names)
    if forward_oauth_token:
        display_env_names.append("CLAUDE_CODE_OAUTH_TOKEN")

    if agent.skills:
        skills_mod.skills_validate_all(list(agent.skills))

    runtime = bottle.runtime
    if runtime == "runsc":
        docker_mod.require_runsc()

    ssh_entries = bottle.ssh
    if ssh_entries:
        ssh_mod.ssh_validate_entries(ssh_entries)

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

    # cleanup state — populated as resources come up.
    state: dict[str, str] = {
        "container": "",
        "pipelock": "",
        "internal_network": "",
        "egress_network": "",
    }

    def cleanup_all() -> None:
        try:
            if state["container"] and docker_mod.container_exists(state["container"]):
                subprocess.run(
                    ["docker", "rm", "-f", state["container"]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if state["pipelock"]:
                pipelock.pipelock_stop(slug)
            if state["internal_network"]:
                network_mod.network_remove(state["internal_network"])
            if state["egress_network"]:
                network_mod.network_remove(state["egress_network"])
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    try:
        pipelock.pipelock_write_yaml(manifest, bottle_name, pipelock_yaml)
        allowlist_summary = pipelock.pipelock_allowlist_summary(manifest, bottle_name)

        env_resolve(manifest, name, env_file, args_file)

        prompt_content = agent.prompt
        prompt_file.write_text(prompt_content)
        prompt_first_line = prompt_content.splitlines()[0] if prompt_content else ""

        # --- Plan + confirm ---
        print(file=sys.stderr)
        info(f"agent           : {name}")
        info(f"image           : {image}")
        if derived_image:
            info(f"cwd             : {USER_CWD} -> /home/node/workspace (derived: {derived_image})")
        info(f"container       : {container}")
        info(f"stage dir       : {stage_dir}")
        info(
            "env (names only): "
            + (", ".join(display_env_names) if display_env_names else "(none)")
        )
        info("skills          : " + (" ".join(agent.skills) if agent.skills else "(none)"))
        info(f"bottle             : {bottle_name}")
        info(f"  runtime       : {runtime}{' (gVisor)' if runtime == 'runsc' else ''}")
        if ssh_entries:
            ssh_names = ", ".join(e.Host for e in ssh_entries)
            info(f"  ssh hosts     : {ssh_names}")
        else:
            info("  ssh hosts     : (none)")
        info(f"  egress        : {allowlist_summary}")
        info(
            f"prompt          : {len(prompt_content)} chars; "
            f"first line: {prompt_first_line or '(empty)'}"
        )
        info("remote-control  : " + ("enabled" if args.remote_control else "disabled"))
        print(file=sys.stderr)

        if dry_run:
            info("dry-run requested; not starting container.")
            cleanup_all()
            return 0

        sys.stderr.write("claude-bottle: launch this agent? [y/N] ")
        sys.stderr.flush()
        reply = read_tty_line()
        if reply not in ("y", "Y", "yes", "YES"):
            info("aborted by user")
            cleanup_all()
            return 0

        # --- Build & launch ---
        docker_mod.build_image(image, REPO_DIR)
        if derived_image:
            docker_mod.build_image_with_cwd(derived_image, image, USER_CWD)

        state["internal_network"] = network_mod.network_create_internal(slug)
        state["egress_network"] = network_mod.network_create_egress(slug)
        state["pipelock"] = pipelock.pipelock_start(
            slug,
            state["internal_network"],
            state["egress_network"],
            stage_dir,
            pipelock_yaml_filename,
        )

        proxy_url = pipelock.pipelock_proxy_url(slug)
        docker_args: list[str] = [
            "--rm", "-d",
            "--name", container,
            "--network", state["internal_network"],
            "-e", f"HTTPS_PROXY={proxy_url}",
            "-e", f"HTTP_PROXY={proxy_url}",
            "-e", "NO_PROXY=localhost,127.0.0.1",
        ]
        if runtime != "runc":
            docker_args.extend(["--runtime", runtime])
        if env_file.stat().st_size > 0:
            docker_args.extend(["--env-file", str(env_file)])

        # ARGS_FILE pairs (-e, NAME) line-by-line.
        args_lines = args_file.read_text().splitlines()
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

        if forward_oauth_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_BOTTLE_OAUTH_TOKEN"]
            docker_args.extend(["-e", "CLAUDE_CODE_OAUTH_TOKEN"])

        docker_args.extend([runtime_image, "sleep", "infinity"])

        info(f"starting container {container} from {runtime_image}")
        # Retry-on-name-conflict loop to mirror the bash version.
        while True:
            full_argv = ["docker", "run", *docker_args]
            run_result = subprocess.run(full_argv, capture_output=True, text=True)
            if run_result.returncode == 0:
                state["container"] = container
                break
            err_text = run_result.stderr
            if pinned_container or "is already in use" not in err_text:
                sys.stderr.write(err_text + "\n")
                die(f"docker run failed for container '{container}'")
            if suffix > 100:
                die(
                    f"could not find a free container name after "
                    f"{default_container}-99 retries; clean up old containers"
                )
            container = f"{default_container}-{suffix}"
            suffix += 1
            # Replace --name slot in docker_args.
            name_idx = docker_args.index("--name") + 1
            docker_args[name_idx] = container
            info(f"name conflict; retrying as {container}")

        container_prompt_path = (
            os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
            + "/.claude-bottle-prompt.txt"
        )
        subprocess.run(
            ["docker", "cp", str(prompt_file), f"{container}:{container_prompt_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        # `docker cp` preserves host UID; re-own/mode as root in the container
        # so node can read its own mode-600 prompt regardless of host UID.
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

        if agent.skills:
            skills_mod.skills_copy_into(container, list(agent.skills))

        if ssh_entries:
            proxy_host_port = pipelock.pipelock_proxy_host_port(slug)
            ssh_mod.ssh_setup(container, stage_dir, proxy_host_port, ssh_entries)

        if args.cwd and Path(USER_CWD, ".git").is_dir():
            info(f"copying {USER_CWD}/.git -> {container}:/home/node/workspace/.git")
            subprocess.run(
                ["docker", "cp", f"{USER_CWD}/.git", f"{container}:/home/node/workspace/.git"],
                stdout=subprocess.DEVNULL,
                check=True,
            )
            subprocess.run(
                ["docker", "exec", "-u", "0", container, "chown", "-R", "node:node", "/home/node/workspace/.git"],
                stdout=subprocess.DEVNULL,
                check=True,
            )

        info(
            "attaching interactive claude session "
            "(Ctrl-D or 'exit' to leave; container will be removed)"
        )
        claude_args = ["--dangerously-skip-permissions"]
        if args.remote_control:
            claude_args.append("--remote-control")
        if prompt_content:
            subprocess.run(
                [
                    "docker", "exec", "-it", container, "claude",
                    *claude_args,
                    "--append-system-prompt-file", container_prompt_path,
                ]
            )
        else:
            subprocess.run(
                ["docker", "exec", "-it", container, "claude", *claude_args]
            )
        info(f"session ended; container {container} will be removed")
        return 0
    finally:
        cleanup_all()
