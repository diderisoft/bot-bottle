"""Manifest dataclasses. Read claude-bottle.json (cwd + $HOME, deep-merged)
into a frozen, validated Manifest tree.

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

Validation runs once at construction (Manifest.from_json_obj) so getters
can trust the shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, cast

from .log import die


def _empty_str_dict() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class SshEntry:
    Host: str
    IdentityFile: str
    Hostname: str = ""
    User: str = ""
    Port: str = ""
    KnownHostKey: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "SshEntry":
        d = _as_json_object(raw, f"bottle '{bottle_name}' ssh[{idx}]")
        host = d.get("Host")
        if not isinstance(host, str) or not host:
            die(f"bottle '{bottle_name}' ssh[{idx}] missing required string field 'Host'")
        ident = d.get("IdentityFile")
        if not isinstance(ident, str) or not ident:
            die(
                f"bottle '{bottle_name}' ssh '{host}' missing required string field "
                f"'IdentityFile'"
            )
        hostname = _opt_str(d.get("Hostname"), f"bottle '{bottle_name}' ssh '{host}' Hostname")
        user = _opt_str(d.get("User"), f"bottle '{bottle_name}' ssh '{host}' User")
        port = _opt_port(d.get("Port"), f"bottle '{bottle_name}' ssh '{host}' Port")
        khk = _opt_str(
            d.get("KnownHostKey"),
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
    def from_dict(cls, bottle_name: str, raw: object) -> "BottleEgress":
        d = _as_json_object(raw, f"bottle '{bottle_name}' egress")
        allow = d.get("allowlist")
        if allow is None:
            return cls()
        if not isinstance(allow, list):
            die(
                f"bottle '{bottle_name}' egress.allowlist must be an array "
                f"(was {type(allow).__name__})"
            )
        items: list[str] = []
        allow_list = cast(list[object], allow)
        for i, host in enumerate(allow_list):
            if not isinstance(host, str):
                die(
                    f"bottle '{bottle_name}' egress.allowlist[{i}] must be a string "
                    f"(was {type(host).__name__})"
                )
            items.append(host)
        return cls(allowlist=tuple(items))


@dataclass(frozen=True)
class Bottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    ssh: tuple[SshEntry, ...] = ()
    egress: BottleEgress = field(default_factory=BottleEgress)

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "Bottle":
        d = _as_json_object(raw, f"bottle '{name}'")

        if "runtime" in d:
            die(
                f"bottle '{name}' has a 'runtime' field, which is no longer "
                f"supported. gVisor (runsc) is now auto-detected by the "
                f"backend; remove the 'runtime' field from the bottle "
                f"definition."
            )

        env: dict[str, str] = {}
        env_raw = d.get("env")
        if env_raw is not None:
            env_dict = _as_json_object(env_raw, f"bottle '{name}' env")
            for var, value in env_dict.items():
                if not isinstance(value, str):
                    die(
                        f"env entry {var} in bottle '{name}' must be a JSON string "
                        f"(was {type(value).__name__}). Use \"?<message>\" for prompt-at-runtime."
                    )
                env[var] = value

        ssh: tuple[SshEntry, ...] = ()
        ssh_raw = d.get("ssh")
        if ssh_raw is not None:
            if not isinstance(ssh_raw, list):
                die(f"bottle '{name}' ssh must be an array (was {type(ssh_raw).__name__})")
            ssh_list = cast(list[object], ssh_raw)
            ssh = tuple(
                SshEntry.from_dict(name, i, entry)
                for i, entry in enumerate(ssh_list)
            )

        egress_raw = d.get("egress")
        egress = (
            BottleEgress.from_dict(name, egress_raw)
            if egress_raw is not None
            else BottleEgress()
        )

        return cls(env=env, ssh=ssh, egress=egress)


@dataclass(frozen=True)
class Agent:
    bottle: str
    skills: tuple[str, ...] = ()
    prompt: str = ""

    @classmethod
    def from_dict(cls, name: str, raw: object, bottle_names: set[str]) -> "Agent":
        d = _as_json_object(raw, f"agent '{name}'")

        bottle = d.get("bottle")
        if not isinstance(bottle, str) or not bottle:
            die(f"agent '{name}' must declare a 'bottle' field naming a defined bottle")
        if bottle not in bottle_names:
            available = ", ".join(sorted(bottle_names)) or "(none defined)"
            die(
                f"agent '{name}' references bottle '{bottle}', which is not defined. "
                f"Available: {available}"
            )

        skills: tuple[str, ...] = ()
        skills_raw = d.get("skills")
        if skills_raw is not None:
            if not isinstance(skills_raw, list):
                die(f"agent '{name}' skills must be an array (was {type(skills_raw).__name__})")
            collected: list[str] = []
            skills_list = cast(list[object], skills_raw)
            for i, skill in enumerate(skills_list):
                if not isinstance(skill, str):
                    die(
                        f"agent '{name}' skills[{i}] must be a string "
                        f"(was {type(skill).__name__})"
                    )
                collected.append(skill)
            skills = tuple(collected)

        prompt_raw = d.get("prompt")
        if prompt_raw is None:
            prompt = ""
        elif isinstance(prompt_raw, str):
            prompt = prompt_raw
        else:
            die(f"agent '{name}' prompt must be a string (was {type(prompt_raw).__name__})")

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

        h: dict[str, object] = home_doc if home_doc is not None else {}
        c: dict[str, object] = cwd_doc if cwd_doc is not None else {}
        h_bottles = _section_dict(h.get("bottles"), "bottles")
        c_bottles = _section_dict(c.get("bottles"), "bottles")
        h_agents = _section_dict(h.get("agents"), "agents")
        c_agents = _section_dict(c.get("agents"), "agents")
        merged: dict[str, object] = {
            "bottles": {**h_bottles, **c_bottles},
            "agents": {**h_agents, **c_agents},
        }
        return cls.from_json_obj(merged)

    @classmethod
    def from_json_obj(cls, obj: object) -> "Manifest":
        """Validate and build a Manifest from a raw JSON-like dict."""
        d = _as_json_object(obj, "manifest")
        raw_bottles = _section_dict(d.get("bottles"), "manifest 'bottles'")
        raw_agents = _section_dict(d.get("agents"), "manifest 'agents'")

        bottles: dict[str, Bottle] = {
            n: Bottle.from_dict(n, b) for n, b in raw_bottles.items()
        }
        bottle_names = set(bottles.keys())
        agents: dict[str, Agent] = {
            n: Agent.from_dict(n, a, bottle_names) for n, a in raw_agents.items()
        }
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


def _as_json_object(value: object, label: str) -> dict[str, object]:
    """Assert that `value` is a JSON object (str-keyed dict) and return
    a view typed as `dict[str, object]` so downstream `.get(...)` calls
    have a typed surface."""
    if not isinstance(value, dict):
        die(f"{label} must be a JSON object (was {type(value).__name__})")
    items = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for k, v in items.items():
        if not isinstance(k, str):
            die(f"{label} keys must be strings (found {type(k).__name__})")
        out[k] = v
    return out


def _section_dict(value: object, label: str) -> dict[str, object]:
    """Like _as_json_object but treats absent/null as an empty section."""
    if value is None:
        return {}
    return _as_json_object(value, label)


def _load_json_or_die(path: Path) -> dict[str, object]:
    try:
        with path.open() as f:
            doc: object = json.load(f)
    except json.JSONDecodeError:
        die(f"claude-bottle.json at {path} is not valid JSON")
    return _as_json_object(doc, f"claude-bottle.json at {path}")


def _opt_str(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        die(f"{label} must be a string (was {type(value).__name__})")
    return value


def _opt_port(value: object, label: str) -> str:
    """Port accepts string or int (JSON-friendly) and is normalized to str."""
    if value is None:
        return ""
    if isinstance(value, bool):
        die(f"{label} must be a string or number (was boolean)")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    die(f"{label} must be a string or number (was {type(value).__name__})")
