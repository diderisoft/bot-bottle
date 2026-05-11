"""Manifest helpers. Read claude-bottle.json and pull the definition for a
named agent.

Schema (see CLAUDE.md "Intended design"):
  {
    "bottles": {
      "<bottle-name>": {
        "env":     { "<NAME>": <env-entry>, ... },
        "ssh":     [ <ssh-entry>, ... ],
        "egress":  { "allowlist": [ "<hostname>", ... ] }
      }
    },
    "agents": {
      "<agent-name>": {
        "skills":  [ "<skill-name>", ... ],
        "prompt":  "<string>",
        "bottle":  "<bottle-name>"
      }
    }
  }

Bottles group shared infrastructure (SSH keys, known hosts, egress allowlist)
that multiple agents can reference. Every agent must reference a bottle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from .log import die


class SshEntry(TypedDict, total=False):
    """One entry from bottle.ssh. Host and IdentityFile are required at
    runtime (enforced by manifest_validate); the rest configure the
    generated ~/.ssh/config block inside the container."""

    Host: str
    IdentityFile: str
    Hostname: str
    User: str
    Port: int | str
    KnownHostKey: str


class BottleEgress(TypedDict, total=False):
    allowlist: list[str]


class Bottle(TypedDict, total=False):
    """Shared infrastructure referenced by agents. env values are
    sentinel-prefixed strings (see env_resolve); runtime defaults to
    "runc" when absent."""

    env: dict[str, str]
    ssh: list[SshEntry]
    egress: BottleEgress
    runtime: Literal["runc", "runsc"]


class Agent(TypedDict, total=False):
    skills: list[str]
    prompt: str
    bottle: str


class Manifest(TypedDict, total=False):
    bottles: dict[str, Bottle]
    agents: dict[str, Agent]


_SUPPORTED_RUNTIMES: tuple[str, ...] = ("runc", "runsc")


def manifest_resolve(cwd: str) -> Manifest:
    """Look for claude-bottle.json in <cwd> and in $HOME, deep-merge
    them (cwd entries override home entries on key conflict for both
    bottles and agents), then validate. Dies if neither file is found,
    either is invalid JSON, or the merged shape violates the schema."""
    cwd_file = Path(cwd) / "claude-bottle.json"
    home_file = Path(os.environ["HOME"]) / "claude-bottle.json"

    cwd_doc = _load_json_or_die(cwd_file) if cwd_file.is_file() else None
    home_doc = _load_json_or_die(home_file) if home_file.is_file() else None

    if cwd_doc is None and home_doc is None:
        die(f"no claude-bottle.json found in {cwd} or {os.environ['HOME']}")

    h = home_doc if home_doc is not None else cast(Manifest, {})
    c = cwd_doc if cwd_doc is not None else cast(Manifest, {})
    manifest: Manifest = {
        "bottles": {**(h.get("bottles") or {}), **(c.get("bottles") or {})},
        "agents": {**(h.get("agents") or {}), **(c.get("agents") or {})},
    }
    manifest_validate(manifest)
    return manifest


def manifest_validate(manifest: Manifest) -> None:
    """Deep-validate the manifest, dying on the first schema violation.
    After this returns, getters can trust that bottles and agents are
    dicts, every agent has a 'bottle' field that references a defined
    bottle, and optional fields (when present) have correct types."""
    bottles = manifest.get("bottles") or {}
    agents = manifest.get("agents") or {}

    for bname, bottle in bottles.items():
        _validate_bottle(bname, bottle)

    for aname, agent in agents.items():
        _validate_agent(aname, agent, bottles)


def _validate_bottle(name: str, bottle: Any) -> None:
    if not isinstance(bottle, dict):
        die(f"bottle '{name}' must be a JSON object (was {_json_type(bottle)})")
    bottle = cast(dict[Any, Any], bottle)

    env = bottle.get("env")
    if env is not None:
        if not isinstance(env, dict):
            die(f"bottle '{name}' env must be a JSON object (was {_json_type(env)})")
        env = cast(dict[Any, Any], env)
        for var, value in env.items():
            if not isinstance(value, str):
                die(
                    f"env entry {var} in bottle '{name}' must be a JSON string "
                    f"(was {_json_type(value)}). Use \"?<message>\" for prompt-at-runtime."
                )

    ssh = bottle.get("ssh")
    if ssh is not None:
        if not isinstance(ssh, list):
            die(f"bottle '{name}' ssh must be an array (was {_json_type(ssh)})")
        ssh = cast(list[Any], ssh)
        for i, entry in enumerate(ssh):
            if not isinstance(entry, dict):
                die(
                    f"bottle '{name}' ssh[{i}] must be a JSON object "
                    f"(was {_json_type(entry)})"
                )
            entry = cast(dict[Any, Any], entry)
            host = entry.get("Host")
            if not isinstance(host, str) or not host:
                die(f"bottle '{name}' ssh[{i}] missing required string field 'Host'")
            ident = entry.get("IdentityFile")
            if not isinstance(ident, str) or not ident:
                die(
                    f"bottle '{name}' ssh '{host}' missing required string field "
                    f"'IdentityFile'"
                )

    egress = bottle.get("egress")
    if egress is not None:
        if not isinstance(egress, dict):
            die(f"bottle '{name}' egress must be a JSON object (was {_json_type(egress)})")
        egress = cast(dict[Any, Any], egress)
        allowlist = egress.get("allowlist")
        if allowlist is not None:
            if not isinstance(allowlist, list):
                die(
                    f"bottle '{name}' egress.allowlist must be an array "
                    f"(was {_json_type(allowlist)})"
                )
            allowlist = cast(list[Any], allowlist)
            for i, host in enumerate(allowlist):
                if not isinstance(host, str):
                    die(
                        f"bottle '{name}' egress.allowlist[{i}] must be a string "
                        f"(was {_json_type(host)})"
                    )

    runtime = bottle.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, str):
            die(f"bottle '{name}' runtime must be a string (was {_json_type(runtime)})")
        if runtime not in _SUPPORTED_RUNTIMES:
            die(
                f"bottle '{name}' runtime '{runtime}' is not supported. "
                f"Use one of: {', '.join(_SUPPORTED_RUNTIMES)}."
            )


def _validate_agent(name: str, agent: Any, bottles: dict[str, Any]) -> None:
    if not isinstance(agent, dict):
        die(f"agent '{name}' must be a JSON object (was {_json_type(agent)})")
    agent = cast(dict[Any, Any], agent)

    skills = agent.get("skills")
    if skills is not None:
        if not isinstance(skills, list):
            die(f"agent '{name}' skills must be an array (was {_json_type(skills)})")
        skills = cast(list[Any], skills)
        for i, skill in enumerate(skills):
            if not isinstance(skill, str):
                die(f"agent '{name}' skills[{i}] must be a string (was {_json_type(skill)})")

    prompt = agent.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        die(f"agent '{name}' prompt must be a string (was {_json_type(prompt)})")

    bottle = agent.get("bottle")
    if not isinstance(bottle, str) or not bottle:
        die(f"agent '{name}' must declare a 'bottle' field naming a defined bottle")
    if bottle not in bottles:
        available = ", ".join(bottles.keys()) or "(none defined)"
        die(
            f"agent '{name}' references bottle '{bottle}', which is not defined. "
            f"Available: {available}"
        )


def _load_json_or_die(path: Path) -> dict[Any, Any]:
    """Load and shallow-check. Confirms bottles/agents (when present)
    are JSON objects so the merge in manifest_resolve cannot TypeError
    before manifest_validate runs."""
    doc: Any = None
    try:
        with path.open() as f:
            doc = json.load(f)
    except json.JSONDecodeError:
        die(f"claude-bottle.json at {path} is not valid JSON")
    if not isinstance(doc, dict):
        die(f"claude-bottle.json at {path} must be a JSON object")
    return cast(dict[Any, Any], doc)


def manifest_has_agent(manifest: Manifest, name: str) -> bool:
    return name in manifest["agents"]


def manifest_require_agent(manifest: Manifest, name: str) -> None:
    """Like has_agent but dies with the available agent names listed."""
    if manifest_has_agent(manifest, name):
        return
    available = ", ".join(manifest["agents"].keys())
    if available:
        die(f"agent '{name}' not defined in claude-bottle.json. Available: {available}")
    else:
        die(f"agent '{name}' not defined in claude-bottle.json (manifest is empty).")


def manifest_env_names(manifest: Manifest, name: str) -> list[str]:
    """Names (not values) of bottles[agent.bottle].env, in declaration
    order. Empty if the agent's bottle has no env."""
    bottle_name = manifest["agents"][name]["bottle"]
    return list(manifest["bottles"][bottle_name].get("env", {}).keys())


def manifest_env_entry(manifest: Manifest, agent: str, var: str) -> str:
    """Raw string value of one env entry. Used by env_resolve, which
    classifies the result by sentinel."""
    bottle_name = manifest["agents"][agent]["bottle"]
    return manifest["bottles"][bottle_name]["env"][var]


def manifest_skills(manifest: Manifest, name: str) -> list[str]:
    return list(manifest["agents"][name].get("skills", []))


def manifest_prompt(manifest: Manifest, name: str) -> str:
    return manifest["agents"][name].get("prompt", "")


def manifest_agent_bottle(manifest: Manifest, name: str) -> str:
    return manifest["agents"][name]["bottle"]


def manifest_has_bottle(manifest: Manifest, bottle_name: str) -> bool:
    return bottle_name in manifest["bottles"]


def manifest_require_bottle(manifest: Manifest, bottle_name: str) -> None:
    if manifest_has_bottle(manifest, bottle_name):
        return
    available = ", ".join(manifest["bottles"].keys())
    if available:
        die(
            f"bottle '{bottle_name}' not defined in claude-bottle.json. "
            f"Available bottles: {available}"
        )
    else:
        die(f"bottle '{bottle_name}' not defined in claude-bottle.json (no bottles defined).")


def manifest_bottle_ssh(manifest: Manifest, bottle_name: str) -> list[SshEntry]:
    return list(manifest["bottles"][bottle_name].get("ssh", []))


def manifest_bottle_runtime(manifest: Manifest, bottle_name: str) -> str:
    """Container runtime for the bottle's agent container. Returns
    "runc" (Docker default) or "runsc" (gVisor opt-in)."""
    return manifest["bottles"][bottle_name].get("runtime", "runc")


def manifest_bottle_egress_allowlist(manifest: Manifest, bottle_name: str) -> list[str]:
    """Hostnames in bottles[bottle_name].egress.allowlist."""
    return list(manifest["bottles"][bottle_name].get("egress", {}).get("allowlist", []))


def manifest_ssh(manifest: Manifest, agent_name: str) -> list[SshEntry]:
    """SSH entries resolved via the agent's "bottle" field."""
    bottle_name = manifest_agent_bottle(manifest, agent_name)
    return manifest_bottle_ssh(manifest, bottle_name)


def _json_type(value: Any) -> str:
    """Mirror jq's type names for parity with the bash error messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
