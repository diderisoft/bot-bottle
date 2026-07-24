"""init: interactively create a new agent and add it to bot-bottle.json."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from ...log import die, info, warn
from .._common import PROG, USER_CWD, read_tty_line


def cmd_init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} init", add_help=True)
    parser.add_argument("scope", choices=["user", "project"])
    args = parser.parse_args(argv)

    if args.scope == "user":
        target_file = Path(os.environ["HOME"]) / "bot-bottle.json"
    else:
        target_file = Path(USER_CWD) / "bot-bottle.json"

    print(file=sys.stderr)
    info(f"bot-bottle init — adding a new agent to {target_file}")
    print(file=sys.stderr)

    # Agent name
    agent_name = ""
    while not agent_name:
        sys.stderr.write("Agent name: ")
        sys.stderr.flush()
        agent_name = read_tty_line().replace(" ", "-")
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
                f'bot-bottle: agent "{agent_name}" already exists in '
                f'{target_file}. Overwrite? [y/N] '
            )
            sys.stderr.flush()
            ow = read_tty_line()
            if ow not in ("y", "Y", "yes", "YES"):
                info("aborted")
                return 0

    # Skills
    print(file=sys.stderr)
    sys.stderr.write("Skills (space or comma separated, or Enter for none): ")
    sys.stderr.flush()
    skills_input = read_tty_line()
    skill_list: list[str] = []
    if skills_input:
        cleaned = skills_input.replace(",", " ")
        skill_list = [s for s in cleaned.split() if s]

    # Prompt
    print(file=sys.stderr)
    info(
        "System prompt — enter text, then a lone '.' on its own line to "
        "finish (just '.' to leave empty):"
    )
    prompt_lines: list[str] = []
    while True:
        line = read_tty_line()
        if line == ".":
            break
        prompt_lines.append(line)
    prompt_content = "\n".join(prompt_lines)

    # Bottle association
    print(file=sys.stderr)
    sys.stderr.write("Associate this agent with a bottle? [y/N] ")
    sys.stderr.flush()
    bottle_yn = read_tty_line()
    bottle_name = ""
    bottle_env: dict[str, str] = {}
    bottle_ssh: list[dict[str, Any]] = []
    bottle_exists_already = False
    if bottle_yn in ("y", "Y", "yes", "YES"):
        while not bottle_name:
            sys.stderr.write("  Bottle name: ")
            sys.stderr.flush()
            bottle_name = read_tty_line().replace(" ", "-")
            if not bottle_name:
                warn("bottle name cannot be empty")

        if bottle_name in (existing.get("bottles") or {}):
            bottle_exists_already = True
            info(
                f"Bottle '{bottle_name}' already exists in {target_file}; "
                f"agent will reference it."
            )
        else:
            info(f"Creating new bottle '{bottle_name}'.")
            bottle_env = _prompt_for_env_vars()
            sys.stderr.write("  Add SSH host entries to this bottle? [y/N] ")
            sys.stderr.flush()
            ssh_yn = read_tty_line()
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
    info(
        "Env vars — enter each var name then its mode. Press Enter with "
        "no name to finish."
    )
    info(
        "  Modes:  secret (prompt at runtime) | interpolated (read from "
        "host env) | literal (hardcoded value)"
    )
    out: dict[str, str] = {}
    while True:
        print(file=sys.stderr)
        sys.stderr.write("  Var name (or Enter to finish): ")
        sys.stderr.flush()
        vname = read_tty_line()
        if not vname:
            break
        sys.stderr.write("  Mode [secret/interpolated/literal] (default: secret): ")
        sys.stderr.flush()
        vmode = read_tty_line() or "secret"
        if vmode == "secret":
            sys.stderr.write(f'  Prompt message shown to user (default: "enter {vname}"): ')
            sys.stderr.flush()
            smsg = read_tty_line()
            value = f"?{smsg}" if smsg else "?"
        elif vmode == "interpolated":
            sys.stderr.write(f"  Host env var to read from (default: {vname}): ")
            sys.stderr.flush()
            hvar = read_tty_line() or vname
            value = "${" + hvar + "}"
        elif vmode == "literal":
            sys.stderr.write("  Value: ")
            sys.stderr.flush()
            value = read_tty_line()
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
        shost = read_tty_line()
        if not shost:
            break
        sys.stderr.write("    Hostname (actual hostname or IP): ")
        sys.stderr.flush()
        shostname = read_tty_line()
        sys.stderr.write("    User: ")
        sys.stderr.flush()
        suser = read_tty_line()
        sys.stderr.write("    Port (default: 22): ")
        sys.stderr.flush()
        sport_raw = read_tty_line() or "22"
        if not sport_raw.isdigit():
            warn("port must be a number; defaulting to 22")
            sport_raw = "22"
        sport = int(sport_raw)
        sys.stderr.write("    IdentityFile (path to private key on host): ")
        sys.stderr.flush()
        sidentity = read_tty_line()
        sys.stderr.write("    KnownHostKey (optional, Enter to skip): ")
        sys.stderr.flush()
        skhk = read_tty_line()

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
