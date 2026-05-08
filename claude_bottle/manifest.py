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
from typing import Any

from .log import die

Manifest = dict[str, Any]


def manifest_resolve(cwd: str) -> Manifest:
    """Look for claude-bottle.json in <cwd> and in $HOME, deep-merge
    them (cwd entries override home entries on key conflict for both
    bottles and agents). Dies if neither file is found or either is
    invalid JSON."""
    cwd_file = Path(cwd) / "claude-bottle.json"
    home_file = Path(os.environ["HOME"]) / "claude-bottle.json"

    cwd_doc = _load_json_or_die(cwd_file) if cwd_file.is_file() else None
    home_doc = _load_json_or_die(home_file) if home_file.is_file() else None

    if cwd_doc is None and home_doc is None:
        die(f"no claude-bottle.json found in {cwd} or {os.environ['HOME']}")

    if cwd_doc is None:
        return home_doc  # type: ignore[return-value]
    if home_doc is None:
        return cwd_doc

    return {
        "bottles": {**(home_doc.get("bottles") or {}), **(cwd_doc.get("bottles") or {})},
        "agents": {**(home_doc.get("agents") or {}), **(cwd_doc.get("agents") or {})},
    }


def _load_json_or_die(path: Path) -> Manifest:
    try:
        with path.open() as f:
            doc = json.load(f)
    except json.JSONDecodeError:
        die(f"claude-bottle.json at {path} is not valid JSON")
    if not isinstance(doc, dict):
        die(f"claude-bottle.json at {path} must be a JSON object")
    return doc


def manifest_has_agent(manifest: Manifest, name: str) -> bool:
    return name in (manifest.get("agents") or {})


def manifest_require_agent(manifest: Manifest, name: str) -> None:
    """Like has_agent but dies with the available agent names listed."""
    if manifest_has_agent(manifest, name):
        return
    available = ", ".join((manifest.get("agents") or {}).keys())
    if available:
        die(f"agent '{name}' not defined in claude-bottle.json. Available: {available}")
    else:
        die(f"agent '{name}' not defined in claude-bottle.json (manifest is empty).")


def manifest_env_names(manifest: Manifest, name: str) -> list[str]:
    """Names (not values) of bottles[agent.bottle].env, in declaration
    order. Empty list if the agent has no bottle or the bottle has no env."""
    agent = (manifest.get("agents") or {}).get(name) or {}
    bottle_name = agent.get("bottle") or ""
    if not bottle_name:
        return []
    bottle = (manifest.get("bottles") or {}).get(bottle_name) or {}
    return list((bottle.get("env") or {}).keys())


def manifest_env_entry(manifest: Manifest, agent: str, var: str) -> str:
    """Raw string value of one env entry. Used by env_resolve, which
    classifies the result by sentinel. Dies if the agent has no bottle,
    or the entry is not a string."""
    agent_def = (manifest.get("agents") or {}).get(agent) or {}
    bottle_name = agent_def.get("bottle") or ""
    if not bottle_name:
        die(f"env entry {var} for agent {agent}: agent has no 'bottle' field")
    bottle = (manifest.get("bottles") or {}).get(bottle_name) or {}
    env = bottle.get("env") or {}
    value = env.get(var)
    if not isinstance(value, str):
        actual = _json_type(value)
        die(
            f"env entry {var} for agent {agent} must be a JSON string "
            f"(was {actual}). Use \"?<message>\" for prompt-at-runtime."
        )
    return value


def manifest_skills(manifest: Manifest, name: str) -> list[str]:
    agent = (manifest.get("agents") or {}).get(name) or {}
    return list(agent.get("skills") or [])


def manifest_prompt(manifest: Manifest, name: str) -> str:
    agent = (manifest.get("agents") or {}).get(name) or {}
    return agent.get("prompt") or ""


def manifest_agent_bottle(manifest: Manifest, name: str) -> str:
    agent = (manifest.get("agents") or {}).get(name) or {}
    return agent.get("bottle") or ""


def manifest_has_bottle(manifest: Manifest, bottle_name: str) -> bool:
    return bottle_name in (manifest.get("bottles") or {})


def manifest_require_bottle(manifest: Manifest, bottle_name: str) -> None:
    if manifest_has_bottle(manifest, bottle_name):
        return
    available = ", ".join((manifest.get("bottles") or {}).keys())
    if available:
        die(
            f"bottle '{bottle_name}' not defined in claude-bottle.json. "
            f"Available bottles: {available}"
        )
    else:
        die(f"bottle '{bottle_name}' not defined in claude-bottle.json (no bottles defined).")


def manifest_bottle_ssh(manifest: Manifest, bottle_name: str) -> list[dict[str, Any]]:
    bottle = (manifest.get("bottles") or {}).get(bottle_name) or {}
    return list(bottle.get("ssh") or [])


def manifest_bottle_egress_allowlist(manifest: Manifest, bottle_name: str) -> list[str]:
    """Hostnames in bottles[bottle_name].egress.allowlist. Dies if the
    field is present but not an array. Per-element string typing is
    re-checked at use-time in pipelock."""
    bottle = (manifest.get("bottles") or {}).get(bottle_name) or {}
    allowlist = (bottle.get("egress") or {}).get("allowlist")
    if allowlist is None:
        return []
    if not isinstance(allowlist, list):
        die(
            f"bottle '{bottle_name}' egress.allowlist must be an array "
            f"(was {_json_type(allowlist)})."
        )
    return list(allowlist)


def manifest_ssh(manifest: Manifest, agent_name: str) -> list[dict[str, Any]]:
    """SSH entries resolved via the agent's "bottle" field; empty if no bottle set."""
    bottle_name = manifest_agent_bottle(manifest, agent_name)
    if not bottle_name:
        return []
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
