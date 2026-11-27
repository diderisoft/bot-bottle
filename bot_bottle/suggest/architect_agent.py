"""The bottle architect's agent (PRD 0117), as bot-bottle runs it.

It's part of bot-bottle, not a file anyone edits: its system prompt (with the
JSON schema its answer follows) is `PROMPT`, its bottle is
`suggest/bottles/bottle-architect.md` (its model's API and nothing else), and
the only choices left to the user are which host runs it
(`bottle-architect.host`) and what on (`bottle-architect.connection`,
`suggest.architect_provider`). `agent_file`
renders it as an agent file on that provider, for the warm bottle to launch.
"""

from __future__ import annotations

ARCHITECT_AGENT = "bottle-architect"

PROMPT = """\
You are bottle-architect, a tool that proposes a bot-bottle bottle for a task.
Your output must be a single JSON object matching this schema exactly — no
prose before or after it:

{
  "summary": "<one plain sentence: what you recommend and why>",
  "option": "<reuse|stack|stack+additions|new-extending|new>",
  "bottles": ["<existing-bottle-name>", ...],
  "new_bottle": null | {"name": "...", "extends": "...", "about": "..."},
  "additions": {
    "egress":      [{"host": "...", "methods": ["GET","POST",...], "why": "..."}],
    "repos":       [{"grant": "<forge>/<owner>/<repo>:write", "why": "..."}],
    "env":         [{"name": "ENV_VAR_NAME", "why": "..."}],
    "credentials": [{"name": "SECRET_NAME", "route": "api.example.com", "why": "..."}]
  },
  "similar": [{"bottle": "...", "covers": 0.9}],
  "notes": ["..."]
}

Rules:
- Prefer reuse > stack > stack+additions > new-extending > new.
- Picking bottles doesn't bring in `basic`: a bottle has only its own
  `extends` chain. List `basic` in `bottles` when the task needs what it
  allows.
- Bias toward too little access; leave out anything uncertain.
- Never propose a passthrough route, never include secret values.
- Grants only on connected forges listed in the input, named by the forge's
  host. A grant needs no bottle stacked for its forge: never stack a bottle
  just to reach a forge, since everything a stacked bottle grants (its
  `repos`, its egress) comes with it.
- Stack a bottle for what the task needs from it, not to reach one thing it
  happens to contain.
- Never propose a bottle with `missing_secrets`: its launch fails.
- Credentials name a secret from the secret names list and the egress route
  they are injected on (the route must also be in additions.egress).
- env names only; the value comes from the secrets file at launch.
"""


# Said where a changed `agents/bottle-architect.md` an earlier `setup` wrote
# is found (setup, doctor): it's an ordinary agent now.
ARCHITECT_NOTE = (f"~/.bot-bottle/agents/{ARCHITECT_AGENT}.md is no longer used by bottle "
                  "suggestions; it's an agent like any other. The architect is part of "
                  "bot-bottle; its host and model are the settings bottle-architect.host "
                  "and bottle-architect.connection (`bot-bottle config`)")


def adoptable_agents() -> list[str]:
    """The host agents an earlier `setup` wrote that bot-bottle now has
    itself, so an unchanged copy is removed (`versions.adopt_builtin_agents`):
    the built-in agents, and the architect."""
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.loader import BUILTIN_AGENTS_DIR, scan_agent_names
    return [*scan_agent_names(BUILTIN_AGENTS_DIR), ARCHITECT_AGENT]


def agent_file(agent_provider: dict[str, object]) -> str:
    """The architect as an agent file on `agent_provider` (its harness and
    login, as `architect_provider.resolve` made them)."""
    # pylint: disable-next=import-outside-toplevel
    from ..starter import render_manifest
    return render_manifest({"agent_provider": agent_provider}, PROMPT)


__all__ = ["ARCHITECT_AGENT", "ARCHITECT_NOTE", "PROMPT", "adoptable_agents", "agent_file"]
