"""The `bottle-architect` agent `setup` writes (PRD 0117).

This is only the starter's content. The suggester reads the agent file
`setup` wrote (`~/.bot-bottle/agents/bottle-architect.md`), so editing that
file changes the architect's prompt, harness and model.
"""

from __future__ import annotations

ARCHITECT_AGENT = "bottle-architect"

_HEADER = """\
# bottle-architect: suggest a bottle for a task (PRD 0117).
# Written by bot-bottle setup. `bot-bottle start`, `bot-bottle bottles
# suggest` and the console's launch page read this file: edit the prompt
# below, set `model:`, or change the harness to tune the proposals. It runs in
# its own bottle (its model API and nothing else), so any harness can be the
# architect.
"""

_PROMPT = """\
You are bottle-architect, a tool that proposes a bot-bottle bottle for a task.
Your output must be a single JSON object matching this schema exactly — no
prose before or after it:

{
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
  `extends` chain (a forge's bottle like `gitea` usually extends nothing).
  List `basic` in `bottles` when the task needs what it allows.
- Bias toward too little access; leave out anything uncertain.
- Never propose inspect:false, never include secret values.
- Grants only on connected forges listed in the input, with a bottle in
  `bottles` whose `forges` declares that forge. Use the smallest bottle that
  declares it (fewest `element_count`, usually the one named after the forge,
  e.g. `gitea`), never a bigger bottle that extends it: everything a stacked
  bottle grants (its `repos`, its egress) comes with it.
- Stack a bottle for what the task needs from it, not to reach one thing it
  happens to contain.
- Never propose a bottle with `missing_secrets`: its launch fails.
- Credentials name a secret from the secret names list and the egress route
  they are injected on (the route must also be in additions.egress).
- env names only; the value comes from the secrets file at launch.
"""



def architect_starter(agent_provider: dict[str, object] | None = None) -> str:
    """The starter, with `agent_provider` (the harness's own starter agent's
    block, as `setup` writes it). It sets no `model:`, so the harness's
    default model answers: measured, a smaller claude model (haiku) was
    slower on this structured prompt than the default."""
    # pylint: disable-next=import-outside-toplevel
    from ..contract.yaml_subset import serialize_yaml_subset
    block = agent_provider or {"harness": "claude", "forward_host_credentials": True}
    return f"---\n{_HEADER}{serialize_yaml_subset({'agent_provider': block})}---\n\n{_PROMPT}"


def starter_for(agent_manifest: str) -> str:
    """The starter on the harness of `agent_manifest`, a provider's starter
    agent file (`create_default_agent_manifest`)."""
    # pylint: disable-next=import-outside-toplevel
    from ..contract.yaml_subset import parse_frontmatter
    block = parse_frontmatter(agent_manifest)[0].get("agent_provider")
    return architect_starter(dict(block) if isinstance(block, dict) else None)


ARCHITECT_STARTER = architect_starter()

__all__ = ["ARCHITECT_AGENT", "ARCHITECT_STARTER", "architect_starter", "starter_for"]
