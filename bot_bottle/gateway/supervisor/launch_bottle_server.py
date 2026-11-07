"""launch-bottle gateway-side helpers: MCP tool definition and arg validation."""
from __future__ import annotations

import json
import re

from bot_bottle.contract import supervise as _sv

_MAX_PROMPT, _MAX_LABEL, _MAX_BOTTLES = 20_000, 128, 8
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

TOOL_DEFINITION: dict[str, object] = {
    "name": _sv.TOOL_LAUNCH_BOTTLE,
    "description": (
        "Ask the operator to launch another agent in a child bottle. "
        "Pass the agent name, an initial prompt, optional bottle names "
        "(empty means the agent's default), an optional label, and a "
        "justification. The operator sees the request with what access "
        "the child would have compared with this bottle's; anything "
        "beyond this bottle's access is removed before launch. The "
        "child can't launch bottles of its own. This bottle may have "
        "at most 3 running children. Work comes back through git: the "
        "child pushes a branch and this bottle fetches it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "agent": {"type": "string",
                      "description": "Name of the agent to launch (must exist on this host)."},
            "prompt": {"type": "string",
                       "description": "Initial task for the child agent (required; headless)."},
            "bottles": {"type": "array", "items": {"type": "string"},
                        "description": "Bottle names to launch with (empty = no bottle grants)."},
            "label": {"type": "string",
                      "description": "Human label for the child (optional)."},
            "justification": {"type": "string",
                              "description": "Why this child bottle is needed."},
        },
        "required": ["agent", "prompt", "justification"],
        "additionalProperties": False,
    },
}


class _ArgError(ValueError):
    """Bad launch-bottle argument; converted to _RpcClientError in server.py."""


def build_proposed(tool: str, args_raw: dict[str, object]) -> str:
    """Validate launch-bottle args and return the proposed_file JSON string."""
    def _err(msg: str) -> _ArgError:
        return _ArgError(f"{tool}: {msg}")
    agent = args_raw.get("agent", "")
    if not isinstance(agent, str) or not _NAME.match(agent):
        raise _err("'agent' must be a valid agent name")
    prompt = args_raw.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _err("'prompt' is required and non-empty")
    if len(prompt) > _MAX_PROMPT:
        raise _err(f"'prompt' is at most {_MAX_PROMPT} characters")
    bottles_raw = args_raw.get("bottles", [])
    if not isinstance(bottles_raw, list) or len(bottles_raw) > _MAX_BOTTLES:
        raise _err(f"'bottles' must be a list of at most {_MAX_BOTTLES}")
    bottles: list[str] = []
    for b in bottles_raw:
        if not isinstance(b, str) or not _NAME.match(b):
            raise _err("each bottle must be a valid bottle name")
        bottles.append(b)
    label = args_raw.get("label", "")
    if not isinstance(label, str) or len(label) > _MAX_LABEL:
        raise _err(f"'label' must be a string of at most {_MAX_LABEL} chars")
    return json.dumps({"agent": agent, "bottles": bottles, "prompt": prompt, "label": label},
                      ensure_ascii=False)
