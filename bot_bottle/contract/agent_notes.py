"""What bot-bottle tells every agent about how its sandbox works.

Fixed text, the same for every bottle: nothing here comes from a bottle's own
configuration (ADR 0003). The prompt file gets `OPERATING_NOTES` appended;
the supervise MCP repeats `GIT_BOUNDARY` in its instructions and tool
descriptions, so an agent sees it whichever it reads.
"""

from __future__ import annotations

GIT_BOUNDARY = (
    "Git doesn't go out through egress. Clone, fetch and push over HTTPS are "
    "blocked even to hosts the egress allowlist lets you reach (a route can "
    "opt in to read-only fetch with `git: {fetch: true}`). Git goes through "
    "git-gate instead: the bottle's own repos keep their normal remote URLs, "
    "which git rewrites to git-gate, and every push is scanned for secrets "
    "there before it's forwarded. A repo the bottle doesn't have can't be "
    "fetched from or pushed to through git-gate until the operator adds it "
    "(ask with the supervise tool `add-repo`); allowing its host for egress "
    "doesn't help."
)

OPERATING_NOTES = (
    "## Working in a bot-bottle sandbox\n\n"
    "This note is added by bot-bottle to every agent.\n\n"
    "- Network access goes through an egress proxy with an allowlist. A "
    "blocked request returns a 403 that says why. If you need a host, ask "
    "with the supervise tools (`list-egress-routes`, then `egress-allow`) "
    "when they're available; the operator decides.\n"
    f"- {GIT_BOUNDARY}\n"
)


def with_operating_notes(prompt: str) -> str:
    """The operator's prompt, then bot-bottle's operating notes."""
    prompt = (prompt or "").rstrip()
    return f"{prompt}\n\n{OPERATING_NOTES}" if prompt else OPERATING_NOTES


__all__ = ["GIT_BOUNDARY", "OPERATING_NOTES", "with_operating_notes"]
