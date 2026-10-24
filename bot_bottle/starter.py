"""Render the starter manifests `bot-bottle setup` writes."""

from __future__ import annotations

from .contract.yaml_subset import serialize_yaml_subset

STARTER_MARKER = "Written by `bot-bottle setup`"


def render_manifest(frontmatter: dict[str, object], body: str) -> str:
    """YAML-subset frontmatter, then the Markdown body."""
    return f"---\n{serialize_yaml_subset(frontmatter)}---\n\n{body.strip()}\n"


def starter_agent(bottle: str, prompt: str) -> str:
    """A starter agent bound to `bottle`; the body is its prompt."""
    return render_manifest({"bottle": bottle}, prompt)


def starter_bottle(agent_provider: dict[str, object], description: str) -> str:
    """A starter bottle with only an `agent_provider` block; its egress is the
    provider's model API route."""
    return render_manifest(
        {"agent_provider": agent_provider},
        f"{description}\n\n{STARTER_MARKER}; edit it freely.",
    )


GENERAL_PROMPT = (
    "You are a coding agent running inside a bot-bottle sandbox. Network "
    "access is limited to your model provider unless the bottle allows more. "
    "Work in the current directory, explain what you change, and ask before "
    "doing anything destructive."
)
