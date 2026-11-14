"""The `bottle-architect` agent (PRD 0117).

It's built in (`manifest/builtin_agents/bottle-architect.md`, PRD 0131): its
harness is `auto`, the first one ready on the host, and it's marked `listed:
false`, so it isn't offered as an agent to launch. A library copy of the same
name replaces it, and is how its prompt, harness or model is changed.
"""

from __future__ import annotations

ARCHITECT_AGENT = "bottle-architect"

__all__ = ["ARCHITECT_AGENT"]
