"""Write a suggested bottle into the host's bottles (PRD 0117): what
`bot-bottle bottles suggest --write` and the console's "Use this bottle" do."""

from __future__ import annotations

from pathlib import Path

from .proposal import Proposal


class SuggestWriteError(ValueError):
    """Why the suggested bottle couldn't be written; the message is shown."""


def write_bottle(name: str, proposal: Proposal, *, root: Path | None = None) -> Path:
    """Write `proposal` as the new host bottle `name` (a new version in the
    bottles history) and return its path. SuggestWriteError if the name isn't
    a bottle name or the bottle exists."""
    # pylint: disable=import-outside-toplevel
    from ..manifest.schema import is_valid_entity_name
    from ..manifest.versions import record_or_warn
    from ..paths import bot_bottle_root

    if not is_valid_entity_name(name):
        raise SuggestWriteError(
            f"{name!r} is not a valid bottle name "
            f"(lowercase letters, digits, hyphens; must start with a letter)")
    root = root or bot_bottle_root()
    path = root / "bottles" / f"{name}.md"
    if path.exists():
        raise SuggestWriteError(f"the bottle {name!r} already exists; pick another name")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bottle_text(proposal))
    record_or_warn(f"suggest: write bottles/{name}", root=root)
    return path


def bottle_text(proposal: Proposal) -> str:
    """The bottle file `proposal` makes: for a reuse or stack, its additions
    only (stacked after its bottles); for a new bottle, with its `extends`."""
    # pylint: disable=import-outside-toplevel
    from ..contract.yaml_subset import serialize_yaml_subset
    from .validate import proposal_to_frontmatter
    fm = proposal_to_frontmatter(proposal)
    if proposal.new_bottle and proposal.new_bottle.about:
        about_lines = [proposal.new_bottle.about]
    else:
        about_lines = [f"Bottle for: {', '.join(proposal.bottles) or 'basic'}"]
    if proposal.notes:
        about_lines.append("")
        about_lines.extend(f"Note: {n}" for n in proposal.notes)
    body = "\n".join(about_lines) + "\n"
    return f"---\n{serialize_yaml_subset(fm)}---\n\n{body}"


__all__ = ["SuggestWriteError", "bottle_text", "write_bottle"]
