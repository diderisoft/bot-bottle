"""Main CLI dispatcher.

Commands: build, cleanup, edit, info, init, list, start
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import docker as docker_mod
from . import network as network_mod
from . import pipelock
from . import skills as skills_mod
from . import ssh as ssh_mod
from .env_resolve import env_resolve
from .log import Die, die, info, warn
from .manifest import (
    manifest_agent_bottle,
    manifest_env_names,
    manifest_prompt,
    manifest_require_agent,
    manifest_require_bottle,
    manifest_resolve,
    manifest_skills,
    manifest_ssh,
)

PROG = "cli.py"
USER_CWD = os.getcwd()
REPO_DIR = str(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_build(_argv: list[str]) -> int:
    docker_mod.require_docker()
    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest")
    docker_mod.build_image(image, REPO_DIR)
    return 0


def cmd_info(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} info", add_help=True)
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    manifest = manifest_resolve(USER_CWD)
    manifest_require_agent(manifest, args.name)

    env_names = manifest_env_names(manifest, args.name)
    skill_names = manifest_skills(manifest, args.name)
    prompt_content = manifest_prompt(manifest, args.name)
    prompt_first_line = prompt_content.splitlines()[0] if prompt_content else ""

    bottle_name = manifest_agent_bottle(manifest, args.name)
    ssh_entries = manifest_ssh(manifest, args.name)

    print()
    info(f"agent           : {args.name}")
    info(f"env (names only): {', '.join(env_names) if env_names else '(none)'}")
    info(f"skills          : {' '.join(skill_names) if skill_names else '(none)'}")
    info(
        f"prompt          : {len(prompt_content)} chars; "
        f"first line: {prompt_first_line or '(empty)'}"
    )
    if bottle_name:
        info(f"bottle             : {bottle_name}")
        if ssh_entries:
            for e in ssh_entries:
                info(
                    f"  ssh host      : {e.get('Host')} "
                    f"(Hostname={e.get('Hostname')}, User={e.get('User')}, "
                    f"Port={e.get('Port')}, IdentityFile={e.get('IdentityFile')})"
                )
                if e.get("KnownHostKey"):
                    info(f"    KnownHostKey: {e['KnownHostKey']}")
        else:
            info("  ssh hosts     : (none)")
    else:
        info("bottle             : (none)")
    print()
    return 0


def cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} list", add_help=True)
    parser.add_argument("scope", choices=["available", "active"])
    args = parser.parse_args(argv)

    if args.scope == "available":
        manifest = manifest_resolve(USER_CWD)
        for name in (manifest.get("agents") or {}).keys():
            print(name)
        return 0

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
        return 0
    print()
    for line in containers.splitlines():
        name, _, status = line.partition("\t")
        info(f"container: {name}  status: {status}")
    print()
    return 0


def cmd_cleanup(_argv: list[str]) -> int:
    docker_mod.require_docker()
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=^claude-bottle-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    containers = (result.stdout or "").strip()
    if not containers:
        info("no active claude-bottle containers")
        return 0
    print(file=sys.stderr)
    for name in containers.splitlines():
        info(f"found: {name}")
    print(file=sys.stderr)
    sys.stderr.write("claude-bottle: remove all of the above? [y/N] ")
    sys.stderr.flush()
    reply = _read_tty_line()
    if reply not in ("y", "Y", "yes", "YES"):
        info("aborted")
        return 0
    for name in containers.splitlines():
        info(f"removing {name}")
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    info("done")
    return 0


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
    manifest = manifest_resolve(USER_CWD)
    manifest_require_agent(manifest, name)

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
    env_names = manifest_env_names(manifest, name)

    # CLAUDE_BOTTLE_OAUTH_TOKEN → CLAUDE_CODE_OAUTH_TOKEN forwarding.
    # Host-side token is always forwarded so every container can authenticate.
    forward_oauth_token = bool(os.environ.get("CLAUDE_BOTTLE_OAUTH_TOKEN"))
    display_env_names = list(env_names)
    if forward_oauth_token:
        display_env_names.append("CLAUDE_CODE_OAUTH_TOKEN")

    skill_names = manifest_skills(manifest, name)
    if skill_names:
        skills_mod.skills_validate_all(skill_names)

    bottle_name = manifest_agent_bottle(manifest, name)
    if not bottle_name:
        die(
            f"agent '{name}' has no 'bottle' field. "
            f"Add a bottle association to this agent in claude-bottle.json."
        )
    manifest_require_bottle(manifest, bottle_name)

    ssh_entries = manifest_ssh(manifest, name)
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

        prompt_content = manifest_prompt(manifest, name)
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
        info("skills          : " + (" ".join(skill_names) if skill_names else "(none)"))
        info(f"bottle             : {bottle_name}")
        if ssh_entries:
            ssh_names = ", ".join(e.get("Host", "") for e in ssh_entries)
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
        reply = _read_tty_line()
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

        if skill_names:
            skills_mod.skills_copy_into(container, skill_names)

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


# ---------------------------------------------------------------------------
# init / edit
# ---------------------------------------------------------------------------


def cmd_init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} init", add_help=True)
    parser.add_argument("scope", choices=["user", "project"])
    args = parser.parse_args(argv)

    if args.scope == "user":
        target_file = Path(os.environ["HOME"]) / "claude-bottle.json"
    else:
        target_file = Path(USER_CWD) / "claude-bottle.json"

    print(file=sys.stderr)
    info(f"claude-bottle init — adding a new agent to {target_file}")
    print(file=sys.stderr)

    # Agent name
    agent_name = ""
    while not agent_name:
        sys.stderr.write("Agent name: ")
        sys.stderr.flush()
        agent_name = _read_tty_line().replace(" ", "-")
        if not agent_name:
            warn("agent name cannot be empty")

    if not re.match(r"^[a-z0-9][a-z0-9-]*$", agent_name):
        warn(
            f"agent name '{agent_name}' contains non-slug characters; "
            f"it will still work but may cause container naming issues"
        )

    existing: dict[str, Any] = {}
    if target_file.is_file():
        try:
            existing = json.loads(target_file.read_text())
        except json.JSONDecodeError:
            die(f"{target_file} exists but is not valid JSON; fix or remove it first")
        if agent_name in (existing.get("agents") or {}):
            sys.stderr.write(
                f'claude-bottle: agent "{agent_name}" already exists in {target_file}. Overwrite? [y/N] '
            )
            sys.stderr.flush()
            ow = _read_tty_line()
            if ow not in ("y", "Y", "yes", "YES"):
                info("aborted")
                return 0

    # Skills
    print(file=sys.stderr)
    sys.stderr.write("Skills (space or comma separated, or Enter for none): ")
    sys.stderr.flush()
    skills_input = _read_tty_line()
    skill_list: list[str] = []
    if skills_input:
        cleaned = skills_input.replace(",", " ")
        skill_list = [s for s in cleaned.split() if s]

    # Prompt
    print(file=sys.stderr)
    info("System prompt — enter text, then a lone '.' on its own line to finish (just '.' to leave empty):")
    prompt_lines: list[str] = []
    while True:
        line = _read_tty_line()
        if line == ".":
            break
        prompt_lines.append(line)
    prompt_content = "\n".join(prompt_lines)

    # Bottle association
    print(file=sys.stderr)
    sys.stderr.write("Associate this agent with a bottle? [y/N] ")
    sys.stderr.flush()
    bottle_yn = _read_tty_line()
    bottle_name = ""
    bottle_env: dict[str, str] = {}
    bottle_ssh: list[dict[str, Any]] = []
    bottle_exists_already = False
    if bottle_yn in ("y", "Y", "yes", "YES"):
        while not bottle_name:
            sys.stderr.write("  Bottle name: ")
            sys.stderr.flush()
            bottle_name = _read_tty_line().replace(" ", "-")
            if not bottle_name:
                warn("bottle name cannot be empty")

        if bottle_name in (existing.get("bottles") or {}):
            bottle_exists_already = True
            info(f"Bottle '{bottle_name}' already exists in {target_file}; agent will reference it.")
        else:
            info(f"Creating new bottle '{bottle_name}'.")
            bottle_env = _prompt_for_env_vars()
            sys.stderr.write("  Add SSH host entries to this bottle? [y/N] ")
            sys.stderr.flush()
            ssh_yn = _read_tty_line()
            if ssh_yn in ("y", "Y", "yes", "YES"):
                bottle_ssh = _prompt_for_ssh_entries()

    # Build agent JSON
    agent_def: dict[str, Any] = {"skills": skill_list, "prompt": prompt_content}
    if bottle_name:
        agent_def["bottle"] = bottle_name

    merged: dict[str, Any] = {
        "bottles": dict(existing.get("bottles") or {}),
        "agents": dict(existing.get("agents") or {}),
    }
    if bottle_name and not bottle_exists_already:
        merged["bottles"][bottle_name] = {"env": bottle_env, "ssh": bottle_ssh}
    merged["agents"][agent_name] = agent_def

    target_file.write_text(json.dumps(merged, indent=2) + "\n")
    info(f"Agent '{agent_name}' written to {target_file}.")
    info(f"Run '{PROG} info {agent_name}' to verify.")
    print(file=sys.stderr)
    return 0


def _prompt_for_env_vars() -> dict[str, str]:
    print(file=sys.stderr)
    info("Env vars — enter each var name then its mode. Press Enter with no name to finish.")
    info("  Modes:  secret (prompt at runtime) | interpolated (read from host env) | literal (hardcoded value)")
    out: dict[str, str] = {}
    while True:
        print(file=sys.stderr)
        sys.stderr.write("  Var name (or Enter to finish): ")
        sys.stderr.flush()
        vname = _read_tty_line()
        if not vname:
            break
        sys.stderr.write("  Mode [secret/interpolated/literal] (default: secret): ")
        sys.stderr.flush()
        vmode = _read_tty_line() or "secret"
        if vmode == "secret":
            sys.stderr.write(f'  Prompt message shown to user (default: "enter {vname}"): ')
            sys.stderr.flush()
            smsg = _read_tty_line()
            value = f"?{smsg}" if smsg else "?"
        elif vmode == "interpolated":
            sys.stderr.write(f"  Host env var to read from (default: {vname}): ")
            sys.stderr.flush()
            hvar = _read_tty_line() or vname
            value = "${" + hvar + "}"
        elif vmode == "literal":
            sys.stderr.write("  Value: ")
            sys.stderr.flush()
            value = _read_tty_line()
        else:
            warn(f"unknown mode '{vmode}'; using secret")
            value = "?"
        out[vname] = value
    return out


def _prompt_for_ssh_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        print(file=sys.stderr)
        sys.stderr.write("    SSH Host alias (or Enter to finish): ")
        sys.stderr.flush()
        shost = _read_tty_line()
        if not shost:
            break
        sys.stderr.write("    Hostname (actual hostname or IP): ")
        sys.stderr.flush()
        shostname = _read_tty_line()
        sys.stderr.write("    User: ")
        sys.stderr.flush()
        suser = _read_tty_line()
        sys.stderr.write("    Port (default: 22): ")
        sys.stderr.flush()
        sport_raw = _read_tty_line() or "22"
        if not sport_raw.isdigit():
            warn("port must be a number; defaulting to 22")
            sport_raw = "22"
        sport = int(sport_raw)
        sys.stderr.write("    IdentityFile (path to private key on host): ")
        sys.stderr.flush()
        sidentity = _read_tty_line()
        sys.stderr.write("    KnownHostKey (optional, Enter to skip): ")
        sys.stderr.flush()
        skhk = _read_tty_line()

        entry: dict[str, Any] = {
            "Host": shost,
            "Hostname": shostname,
            "User": suser,
            "Port": sport,
            "IdentityFile": sidentity,
        }
        if skhk:
            entry["KnownHostKey"] = skhk
        out.append(entry)
    return out


def cmd_edit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} edit", add_help=True)
    parser.add_argument("scope", choices=["user", "project"])
    parser.add_argument("name")
    args = parser.parse_args(argv)

    if args.scope == "user":
        target_file = Path(os.environ["HOME"]) / "claude-bottle.json"
    else:
        target_file = Path(USER_CWD) / "claude-bottle.json"

    if not target_file.is_file():
        die(f"{target_file} does not exist")

    try:
        doc = json.loads(target_file.read_text())
    except json.JSONDecodeError:
        die(f"{target_file} is not valid JSON")

    if args.name not in (doc.get("agents") or {}):
        die(f"agent '{args.name}' not found in {target_file}")

    line = 1
    text = target_file.read_text().splitlines()
    needle = f'"{args.name}"'
    for idx, l in enumerate(text, start=1):
        if needle in l:
            line = idx
            break
    os.execvp("vim", ["vim", f"+{line}", str(target_file)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_tty_line() -> str:
    """Mirror `IFS= read -r REPLY </dev/tty`. Falls back to stdin."""
    try:
        with open("/dev/tty", "r") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


COMMANDS = {
    "build": cmd_build,
    "cleanup": cmd_cleanup,
    "edit": cmd_edit,
    "info": cmd_info,
    "init": cmd_init,
    "list": cmd_list,
    "start": cmd_start,
}


def usage() -> None:
    sys.stderr.write(f"usage: {PROG} <command> [args...]\n\n")
    sys.stderr.write("Commands:\n")
    sys.stderr.write("  build     build (or rebuild) the claude-bottle Docker image\n")
    sys.stderr.write("  cleanup   stop and remove all active claude-bottle containers\n")
    sys.stderr.write("  edit      open an agent in vim for editing\n")
    sys.stderr.write("  info      print env, skills, and prompt details for a named agent\n")
    sys.stderr.write("  init      interactively create a new agent and add it to claude-bottle.json\n")
    sys.stderr.write("  list      list available agents or active containers\n")
    sys.stderr.write("  start     boot a container for a named agent and attach an interactive session\n\n")
    sys.stderr.write(f"Run '{PROG} <command> --help' for command-specific usage.\n")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        usage()
        return 2
    command = argv[0]
    rest = argv[1:]
    if command in ("-h", "--help"):
        usage()
        return 0
    handler = COMMANDS.get(command)
    if handler is None:
        usage()
        die(f"unknown command: {command}")
    try:
        return handler(rest) or 0
    except Die as e:
        return e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
