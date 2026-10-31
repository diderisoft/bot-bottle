"""Render the starter manifests `bot-bottle setup` writes."""

from __future__ import annotations

from .contract.yaml_subset import serialize_yaml_subset

STARTER_MARKER = "Written by `bot-bottle setup`"


def render_manifest(frontmatter: dict[str, object], body: str) -> str:
    """YAML-subset frontmatter, then the Markdown body."""
    return f"---\n{serialize_yaml_subset(frontmatter)}---\n\n{body.strip()}\n"


def starter_agent(prompt: str, agent_provider: dict[str, object]) -> str:
    """A starter agent with its harness and what it needs (`agent_provider`);
    it names no bottle, so a launch picks one. The body is its prompt, so the
    marker is a frontmatter comment. Starters take typing from the console (`remote.writable`), so a
    paired phone can talk to them; an agent written by hand stays view-only
    unless it says so."""
    rendered = render_manifest({"remote": {"writable": True},
                                "agent_provider": agent_provider}, prompt)
    return rendered.replace("---\n", f"---\n# {STARTER_MARKER}; edit it freely.\n", 1)


GENERAL_PROMPT = (
    "You are a coding agent running inside a bot-bottle sandbox. Network "
    "access is limited to your model provider unless the bottle allows more. "
    "Work in the current directory, explain what you change, and ask before "
    "doing anything destructive."
)
