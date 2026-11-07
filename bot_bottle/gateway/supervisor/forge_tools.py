"""The supervise tools for forges (PRD 0105, PRD 0120): `forge-connect`,
`add-repo` and `create-repo`, as `tools/list` shows them."""

from __future__ import annotations

from bot_bottle.contract import supervise as _sv
from bot_bottle.contract.agent_notes import GIT_BOUNDARY

FORGE_TOOL_DEFINITIONS: list[dict[str, object]] = [
    {
        "name": _sv.TOOL_FORGE_CONNECT,
        "description": (
            "Ask the operator to connect a git forge (Gitea or GitHub). "
            "Pass the forge's web URL; unsupported kinds are refused at once. "
            "The operator pastes a token on their phone; you never see it. "
            "On approval the notes say which account connected. This "
            "doesn't give this bottle a repo: "
            + GIT_BOUNDARY
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": ("The forge's web URL, e.g. https://gitea.example.com "
                                        "or https://github.com.")},
                "justification": {"type": "string",
                                  "description": "Why this forge is needed."},
            },
            "required": ["url", "justification"],
            "additionalProperties": False,
        },
    },
    {
        "name": _sv.TOOL_ADD_REPO,
        "description": (
            "Ask the operator for a repo on a forge this host has connected "
            "(`forge-connect` if not). Pass the grant `<forge>/<owner>/<repo>`, "
            "adding `:write` to push; the operator may grant read-only instead. "
            "On approval the repo is added to this running bottle: use its normal "
            "URL and git routes it through git-gate. For a name clash, use "
            "`<alias>=<grant>`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": ("The grant, e.g. gitea.example.com/acme/app:write, "
                                         "github.com/acme/app or gh-app=github.com/acme/app.")},
                "justification": {"type": "string",
                                  "description": "Why this repo is needed."},
            },
            "required": ["repo", "justification"],
            "additionalProperties": False,
        },
    },
    {
        "name": _sv.TOOL_CREATE_REPO,
        "description": (
            "Ask this host to create a new, empty repo on a forge it has "
            "connected (if it hasn't, `forge-connect` first), as the account "
            "this bottle pushes as. Pass `<forge>/<owner>/<name>`; it's private "
            "unless `public` is true. A repo that exists already is refused: "
            "ask for it with `add-repo`. On approval (or at once, when the "
            "bottle allows creating there) it's created and added to this "
            "running bottle with write: push to its normal URL (the notes give "
            "it) and git routes it through git-gate. You never see a token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "The repo to create, e.g. github.com/acme/new-tool."},
                "public": {"type": "boolean",
                           "description": "Make it public (default: private)."},
                "justification": {"type": "string",
                                  "description": "Why this repo is needed."},
            },
            "required": ["repo", "justification"],
            "additionalProperties": False,
        },
    },
]

__all__ = ["FORGE_TOOL_DEFINITIONS"]
