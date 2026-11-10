"""Manifest schema helpers."""

from __future__ import annotations

import re
from pathlib import Path

# Entity filenames are kebab-case starting with a letter; others are skipped
# with a warning.
_FILENAME_RX = re.compile(r"^[a-z][a-z0-9-]*$")


# Accepted frontmatter keys; anything else dies with a "did you mean".
BOTTLE_KEYS = frozenset(
    {
        # `git-gate` is the old spelling, refused with the new one.
        "env", "extends", "agent_provider", "git_gate", "git-gate", "egress",
        "supervise", "nested_containers", "preserve_container", "audit", "credentials",
        "resources",
    }
)
AGENT_KEYS_REQUIRED: frozenset[str] = frozenset()
# `git-gate` and `forge_users` are old forms, refused with their new one.
AGENT_KEYS_OPTIONAL = frozenset({"skills", "identity", "remote", "agent_provider",
                                 "git-gate", "forge_users"})

# The bottle a launch runs in when none is selected. Agents don't name a
# bottle (ADR 0011), so an agent file travels without its host's bottles.
DEFAULT_BOTTLE = "basic"

# Claude Code subagent fields accepted and ignored, so a file can double as
# `~/.claude/agents/*.md`.
CLAUDE_CODE_AGENT_PASSTHROUGH_KEYS = frozenset({
    "name", "description", "model", "color", "memory",
})
AGENT_KEYS = (
    AGENT_KEYS_REQUIRED | AGENT_KEYS_OPTIONAL | CLAUDE_CODE_AGENT_PASSTHROUGH_KEYS
)
AGENT_MODEL_KEYS = AGENT_KEYS | frozenset({"prompt"})


def is_valid_entity_name(name: str) -> bool:
    """True if `name` is kebab-case `[a-z][a-z0-9-]*`, which also makes it safe
    as a path segment."""
    return bool(_FILENAME_RX.match(name))


def entity_name_from_path(path: Path) -> str | None:
    """The entity name from a filename, or None if it doesn't fit the
    convention."""
    if path.suffix != ".md":
        return None
    stem = path.stem
    if not is_valid_entity_name(stem):
        return None
    return stem


def validate_bottle_frontmatter_keys(path: Path, keys: object) -> None:
    _validate_frontmatter_keys("bottle", path, keys, BOTTLE_KEYS)


def validate_agent_frontmatter_keys(path: Path, keys: object) -> None:
    if "bottle" in keys:  # type: ignore[operator]
        reject_agent_bottle(path.stem)
    _validate_frontmatter_keys("agent", path, keys, AGENT_KEYS)


def reject_agent_bottle(agent: str, bottle: object = None) -> None:
    """Raise for an agent that still names a bottle, saying what to do."""
    from .util import ManifestError  # pylint: disable=import-outside-toplevel
    named = isinstance(bottle, str) and bool(bottle)
    raise ManifestError(
        f"agent '{agent}': agents no longer name a bottle"
        f"{f' (`bottle: {bottle}`)' if named else ''}; remove that line and pick "
        f"bottles at launch ({f'`--bottle {bottle}`' if named else '`--bottle <name>`'}; "
        f"none means `{DEFAULT_BOTTLE}`)."
    )


def _validate_frontmatter_keys(
    kind: str,
    path: Path,
    keys: object,
    allowed_keys: frozenset[str],
) -> None:
    from .util import ManifestError

    key_set = set(keys)  # type: ignore
    unknown = key_set - allowed_keys  # type: ignore
    if unknown:
        allowed = ", ".join(sorted(allowed_keys))
        raise ManifestError(
            f"{kind} file {path}: unknown frontmatter key(s) "
            f"{sorted(unknown)}; allowed keys are {allowed}."  # type: ignore
        )
