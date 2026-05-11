"""Manifest dataclasses. Read claude-bottle.json (cwd + $HOME, deep-merged)
into a frozen, validated Manifest tree.

Schema (see CLAUDE.md "Intended design"):
  {
    "bottles": {
      "<bottle-name>": {
        "env":     { "<NAME>": <env-entry>, ... },
        "ssh":     [ <ssh-entry>, ... ],
        "egress":  { "allowlist": [ "<hostname>", ... ] },
        "runtime": "runc" | "runsc"
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

Validation runs once at construction (Manifest.from_json_obj) so getters
can trust the shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from .log import die


_SUPPORTED_RUNTIMES: tuple[str, ...] = ("runc", "runsc")
Runtime = Literal["runc", "runsc"]


@dataclass(frozen=True)
class SshEntry:
    Host: str
    IdentityFile: str
    Hostname: str = ""
    User: str = ""
    Port: str = ""
    KnownHostKey: str = ""

    @classmethod
    def _from_dict(cls, bottle_name: str, idx: int, raw: Any) -> "SshEntry":
        if not isinstance(raw, dict):
            die(
                f"bottle '{bottle_name}' ssh[{idx}] must be a JSON object "
                f"(was {_json_type(raw)})"
            )
        host = raw.get("Host")
        if not isinstance(host, str) or not host:
            die(f"bottle '{bottle_name}' ssh[{idx}] missing required string field 'Host'")
        ident = raw.get("IdentityFile")
        if not isinstance(ident, str) or not ident:
            die(
                f"bottle '{bottle_name}' ssh '{host}' missing required string field "
                f"'IdentityFile'"
            )
        hostname = _opt_str(raw.get("Hostname"), f"bottle '{bottle_name}' ssh '{host}' Hostname")
        user = _opt_str(raw.get("User"), f"bottle '{bottle_name}' ssh '{host}' User")
        port = _opt_port(raw.get("Port"), f"bottle '{bottle_name}' ssh '{host}' Port")
        khk = _opt_str(
            raw.get("KnownHostKey"),
            f"bottle '{bottle_name}' ssh '{host}' KnownHostKey",
        )
        return cls(
            Host=host,
            IdentityFile=ident,
            Hostname=hostname,
            User=user,
            Port=port,
            KnownHostKey=khk,
        )


@dataclass(frozen=True)
class BottleEgress:
    allowlist: tuple[str, ...] = ()

    @classmethod
    def _from_dict(cls, bottle_name: str, raw: Any) -> "BottleEgress":
        if not isinstance(raw, dict):
            die(
                f"bottle '{bottle_name}' egress must be a JSON object "
                f"(was {_json_type(raw)})"
            )
        allow = raw.get("allowlist")
        if allow is None:
            return cls()
        if not isinstance(allow, list):
            die(
                f"bottle '{bottle_name}' egress.allowlist must be an array "
                f"(was {_json_type(allow)})"
            )
        for i, host in enumerate(allow):
            if not isinstance(host, str):
                die(
                    f"bottle '{bottle_name}' egress.allowlist[{i}] must be a string "
                    f"(was {_json_type(host)})"
                )
        return cls(allowlist=tuple(allow))


@dataclass(frozen=True)
class Bottle:
    env: Mapping[str, str] = field(default_factory=dict)
    ssh: tuple[SshEntry, ...] = ()
    egress: BottleEgress = field(default_factory=BottleEgress)
    runtime: Runtime = "runc"

    @classmethod
    def _from_dict(cls, name: str, raw: Any) -> "Bottle":
        if not isinstance(raw, dict):
            die(f"bottle '{name}' must be a JSON object (was {_json_type(raw)})")

        env_raw = raw.get("env")
        env: dict[str, str] = {}
        if env_raw is not None:
            if not isinstance(env_raw, dict):
                die(f"bottle '{name}' env must be a JSON object (was {_json_type(env_raw)})")
            for var, value in env_raw.items():
                if not isinstance(value, str):
                    die(
                        f"env entry {var} in bottle '{name}' must be a JSON string "
                        f"(was {_json_type(value)}). Use \"?<message>\" for prompt-at-runtime."
                    )
                env[var] = value

        ssh_raw = raw.get("ssh")
        ssh: tuple[SshEntry, ...] = ()
        if ssh_raw is not None:
            if not isinstance(ssh_raw, list):
                die(f"bottle '{name}' ssh must be an array (was {_json_type(ssh_raw)})")
            ssh = tuple(
                SshEntry._from_dict(name, i, entry) for i, entry in enumerate(ssh_raw)
            )

        egress_raw = raw.get("egress")
        egress = (
            BottleEgress._from_dict(name, egress_raw)
            if egress_raw is not None
            else BottleEgress()
        )

        runtime_raw = raw.get("runtime")
        if runtime_raw is None:
            runtime: Runtime = "runc"
        else:
            if not isinstance(runtime_raw, str):
                die(f"bottle '{name}' runtime must be a string (was {_json_type(runtime_raw)})")
            if runtime_raw not in _SUPPORTED_RUNTIMES:
                die(
                    f"bottle '{name}' runtime '{runtime_raw}' is not supported. "
                    f"Use one of: {', '.join(_SUPPORTED_RUNTIMES)}."
                )
            runtime = runtime_raw  # type: ignore[assignment]

        return cls(env=env, ssh=ssh, egress=egress, runtime=runtime)


@dataclass(frozen=True)
class Agent:
    bottle: str
    skills: tuple[str, ...] = ()
    prompt: str = ""

    @classmethod
    def _from_dict(cls, name: str, raw: Any, bottle_names: set[str]) -> "Agent":
        if not isinstance(raw, dict):
            die(f"agent '{name}' must be a JSON object (was {_json_type(raw)})")

        bottle = raw.get("bottle")
        if not isinstance(bottle, str) or not bottle:
            die(f"agent '{name}' must declare a 'bottle' field naming a defined bottle")
        if bottle not in bottle_names:
            available = ", ".join(sorted(bottle_names)) or "(none defined)"
            die(
                f"agent '{name}' references bottle '{bottle}', which is not defined. "
                f"Available: {available}"
            )

        skills_raw = raw.get("skills")
        skills: tuple[str, ...] = ()
        if skills_raw is not None:
            if not isinstance(skills_raw, list):
                die(f"agent '{name}' skills must be an array (was {_json_type(skills_raw)})")
            for i, skill in enumerate(skills_raw):
                if not isinstance(skill, str):
                    die(
                        f"agent '{name}' skills[{i}] must be a string "
                        f"(was {_json_type(skill)})"
                    )
            skills = tuple(skills_raw)

        prompt_raw = raw.get("prompt")
        if prompt_raw is None:
            prompt = ""
        elif isinstance(prompt_raw, str):
            prompt = prompt_raw
        else:
            die(f"agent '{name}' prompt must be a string (was {_json_type(prompt_raw)})")

        return cls(bottle=bottle, skills=skills, prompt=prompt)


@dataclass(frozen=True)
class Manifest:
    bottles: Mapping[str, Bottle]
    agents: Mapping[str, Agent]

    @classmethod
    def resolve(cls, cwd: str) -> "Manifest":
        """Look for claude-bottle.json in <cwd> and in $HOME, deep-merge
        them (cwd entries override home entries on key conflict for both
        bottles and agents), then validate. Dies if neither file is
        found, either is invalid JSON, or the merged shape violates the
        schema."""
        cwd_file = Path(cwd) / "claude-bottle.json"
        home_file = Path(os.environ["HOME"]) / "claude-bottle.json"

        cwd_doc = _load_json_or_die(cwd_file) if cwd_file.is_file() else None
        home_doc = _load_json_or_die(home_file) if home_file.is_file() else None

        if cwd_doc is None and home_doc is None:
            die(f"no claude-bottle.json found in {cwd} or {os.environ['HOME']}")

        h = home_doc or {}
        c = cwd_doc or {}
        merged: dict[str, Any] = {
            "bottles": {**(h.get("bottles") or {}), **(c.get("bottles") or {})},
            "agents": {**(h.get("agents") or {}), **(c.get("agents") or {})},
        }
        return cls.from_json_obj(merged)

    @classmethod
    def from_json_obj(cls, obj: Any) -> "Manifest":
        """Validate and build a Manifest from a raw JSON-like dict."""
        if not isinstance(obj, dict):
            die(f"manifest must be a JSON object (was {_json_type(obj)})")

        raw_bottles = obj.get("bottles") or {}
        raw_agents = obj.get("agents") or {}
        if not isinstance(raw_bottles, dict):
            die(f"manifest 'bottles' must be a JSON object (was {_json_type(raw_bottles)})")
        if not isinstance(raw_agents, dict):
            die(f"manifest 'agents' must be a JSON object (was {_json_type(raw_agents)})")

        bottles = {n: Bottle._from_dict(n, b) for n, b in raw_bottles.items()}
        bottle_names = set(bottles.keys())
        agents = {n: Agent._from_dict(n, a, bottle_names) for n, a in raw_agents.items()}
        return cls(bottles=bottles, agents=agents)

    def has_agent(self, name: str) -> bool:
        return name in self.agents

    def require_agent(self, name: str) -> None:
        if self.has_agent(name):
            return
        available = ", ".join(self.agents.keys())
        if available:
            die(f"agent '{name}' not defined in claude-bottle.json. Available: {available}")
        die(f"agent '{name}' not defined in claude-bottle.json (manifest is empty).")

    def has_bottle(self, name: str) -> bool:
        return name in self.bottles

    def require_bottle(self, name: str) -> None:
        if self.has_bottle(name):
            return
        available = ", ".join(self.bottles.keys())
        if available:
            die(
                f"bottle '{name}' not defined in claude-bottle.json. "
                f"Available bottles: {available}"
            )
        die(f"bottle '{name}' not defined in claude-bottle.json (no bottles defined).")

    def bottle_for(self, agent_name: str) -> Bottle:
        """Resolve the Bottle the named agent references. The validator
        guarantees both lookups succeed for a manifest built via
        from_json_obj."""
        return self.bottles[self.agents[agent_name].bottle]


def _load_json_or_die(path: Path) -> dict[str, Any]:
    try:
        with path.open() as f:
            doc = json.load(f)
    except json.JSONDecodeError:
        die(f"claude-bottle.json at {path} is not valid JSON")
    if not isinstance(doc, dict):
        die(f"claude-bottle.json at {path} must be a JSON object")
    return doc


def _opt_str(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        die(f"{label} must be a string (was {_json_type(value)})")
    return value


def _opt_port(value: Any, label: str) -> str:
    """Port accepts string or int (JSON-friendly) and is normalized to str."""
    if value is None:
        return ""
    if isinstance(value, bool):
        die(f"{label} must be a string or number (was boolean)")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    die(f"{label} must be a string or number (was {_json_type(value)})")


def _json_type(value: Any) -> str:
    """Mirror jq's type names for parity with the original bash error messages."""
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
