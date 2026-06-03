"""Internal manifest schema policy helpers."""

from __future__ import annotations

import re
from pathlib import Path

# Filename-as-key uses kebab-case ASCII. The first character is a
# letter so we don't conflict with hidden files / Markdown special
# names (`.md`, `_template.md`, etc.). Filenames that fail this
# pattern are skipped with a warning rather than crashing the load.
_FILENAME_RX = re.compile(r"^[a-z][a-z0-9-]*$")


# Frontmatter keys we accept on each entity. Anything not in these
# sets dies with a "did you mean" pointer: typos should not silently
# ghost into an empty config.
BOTTLE_KEYS = frozenset(
    {"env", "extends", "agent_provider", "git-gate", "egress", "supervise"}
)
AGENT_KEYS_REQUIRED = frozenset({"bottle"})
AGENT_KEYS_OPTIONAL = frozenset({"skills", "git-gate"})

# Claude Code subagent fields bot-bottle ignores at launch but does
# not reject. This lets the same file double as
# `~/.claude/agents/*.md` without modification.
CLAUDE_CODE_AGENT_PASSTHROUGH_KEYS = frozenset({
    "name", "description", "model", "color", "memory",
})
AGENT_KEYS = (
    AGENT_KEYS_REQUIRED | AGENT_KEYS_OPTIONAL | CLAUDE_CODE_AGENT_PASSTHROUGH_KEYS
)
AGENT_MODEL_KEYS = AGENT_KEYS | frozenset({"prompt"})


def entity_name_from_path(path: Path) -> str | None:
    """Return the entity name implied by the filename, or None if the
    filename does not fit the [a-z][a-z0-9-]* convention."""
    if path.suffix != ".md":
        return None
    stem = path.stem
    if not _FILENAME_RX.match(stem):
        return None
    return stem


def validate_bottle_frontmatter_keys(path: Path, keys: object) -> None:
    _validate_frontmatter_keys("bottle", path, keys, BOTTLE_KEYS)


def validate_agent_frontmatter_keys(path: Path, keys: object) -> None:
    _validate_frontmatter_keys("agent", path, keys, AGENT_KEYS)


def _validate_frontmatter_keys(
    kind: str,
    path: Path,
    keys: object,
    allowed_keys: frozenset[str],
) -> None:
    from .manifest_util import ManifestError

    key_set = set(keys)
    unknown = key_set - allowed_keys
    if unknown:
        allowed = ", ".join(sorted(allowed_keys))
        raise ManifestError(
            f"{kind} file {path}: unknown frontmatter key(s) "
            f"{sorted(unknown)}; allowed keys are {allowed}."
        )
