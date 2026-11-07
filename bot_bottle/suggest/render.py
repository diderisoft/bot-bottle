"""Render a Proposal as human-readable output (PRD 0117).

Reuses `bottle_elements` from `control/hostctl/manifest_elements` for the
live-like elements preview.  When bottle dirs are not available (e.g. in
tests) the preview falls back to a plain text listing of the proposal fields.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..contract.yaml_subset import serialize_yaml_subset
from .proposal import Proposal
from .validate import proposal_to_frontmatter, stack_names


def render_proposal(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]] | None = None,
    *,
    json_output: bool = False,
    compact: bool = False,
) -> str:
    """Return the proposal as a human-readable string.

    When `json_output` is True, returns the raw JSON.  Otherwise prints the
    option, bottles, additions and notes, followed by the `bottle_elements`
    preview if `dirs` is given. `compact` lists only what the proposal adds,
    and each inherited bottle as one line of counts (the guided `start`).
    """
    if json_output:
        return proposal.to_json()
    lines: list[str] = [summary_line(proposal), ""]
    lines.append(f"option:  {proposal.option}")
    if proposal.source:
        lines.append(f"from:    {proposal.source}")
    if proposal.bottles:
        lines.append(f"bottles: {', '.join(proposal.bottles)}")
    if proposal.new_bottle:
        nb = proposal.new_bottle
        lines.append(f"new:     {nb.name}" + (f" (extends {nb.extends})" if nb.extends else ""))
        if nb.about:
            lines.append(f"         {nb.about}")
    add = proposal.additions
    if add.egress:
        for e in add.egress:
            meth = "/".join(e.methods) if e.methods else "any"
            why = f"  # {e.why}" if e.why else ""
            lines.append(f"  + egress {e.host} [{meth}]{why}")
    if add.repos:
        for r in add.repos:
            why = f"  # {r.why}" if r.why else ""
            lines.append(f"  + repo   {r.grant}{why}")
    if add.credentials:
        for c in add.credentials:
            why = f"  # {c.why}" if c.why else ""
            lines.append(f"  + cred   {c.name} -> {c.route}{why}")
    if add.env:
        for v in add.env:
            why = f"  # {v.why}" if v.why else ""
            lines.append(f"  + env    {v.name}{why}")
    if proposal.similar:
        for s in proposal.similar:
            lines.append(f"similar: {s.bottle} (covers {int(s.covers * 100)}%)")
    if proposal.notes:
        for note in proposal.notes:
            lines.append(f"note:    {note}")
    if proposal.blocked:
        lines.append(f"can't run: {proposal.blocked}")
    if dirs is not None:
        lines.append("")
        lines.append("elements preview:")
        lines.extend(_elements_preview(proposal, dirs, compact=compact))
    return "\n".join(lines)


def proposal_elements(proposal: Proposal,
                      dirs: list[tuple[Path, bool]]) -> list[dict[str, Any]] | None:
    """The elements a launch with `proposal` would run with (`bottle_elements`:
    its stack's, then its own, `source` `_suggest`); None when they can't be
    loaded."""
    try:
        # pylint: disable-next=import-outside-toplevel
        from ..control.hostctl.manifest_elements import bottle_elements
    except ImportError:
        return None
    fm = proposal_to_frontmatter(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        bottles_dir = Path(tmp) / "bottles"
        bottles_dir.mkdir()
        if fm:
            text = f"---\n{serialize_yaml_subset(fm)}---\n\nSuggested bottle.\n"
            (bottles_dir / "_suggest.md").write_text(text)
            tmp_dirs: list[tuple[Path, bool]] = [(bottles_dir, False)] + list(dirs)
            names = stack_names(proposal, "_suggest")
        else:
            tmp_dirs = list(dirs)
            names = list(proposal.bottles) or ["basic"]
        try:
            return bottle_elements(names, tmp_dirs)
        except Exception:  # pylint: disable=broad-exception-caught
            return None


# What each element kind counts as in a summary, singular and plural.
_COUNTED = {"egress": ("host", "hosts"), "repo": ("repo", "repos"),
            "env": ("env var", "env vars")}


def counted(n: int, kind: str) -> str:
    """`1 host`, `2 hosts`."""
    one, many = _COUNTED.get(kind, (kind, kind + "s"))
    return f"{n} {one if n == 1 else many}"


def added_counts(proposal: Proposal) -> list[str]:
    """What the proposal adds to its stack, counted: `["1 host", "1 repo"]`."""
    add = proposal.additions
    counts = [(len(add.egress), "egress"), (len(add.repos), "repo"), (len(add.env), "env"),
              (len(add.credentials), "credential")]
    return [counted(n, kind) for n, kind in counts if n]


def summary_line(proposal: Proposal) -> str:
    """One plain sentence on what `proposal` recommends: the architect's, else
    one made from the proposal."""
    if proposal.summary:
        return proposal.summary
    nb = proposal.new_bottle
    if nb and nb.name:
        made = f"A new bottle, {nb.name}" + (f", extending {nb.extends}" if nb.extends else "")
        return made + (f": {nb.about.rstrip('.')}." if nb.about else ".")
    adds = added_counts(proposal)
    base = " + ".join(proposal.bottles) or "basic"
    return f"Run in {base}" + (f", adding {_and(adds)}." if adds else ".")


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _elements_preview(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]],
    *,
    compact: bool = False,
) -> list[str]:
    """The proposal's elements (`proposal_elements`) as formatted lines, or a
    line saying they couldn't be loaded."""
    rows = proposal_elements(proposal, dirs)
    if rows is None:
        return ["  (could not load the bottle chain for a preview)"]
    if compact:
        return _compact_preview(proposal, rows)
    lines: list[str] = []
    for row in rows:
        shown = row.get("source")
        if shown == "_suggest":  # the proposal's own bottle, by the name it'd get
            shown = (f"{proposal.new_bottle.name} (new)" if proposal.new_bottle
                     and proposal.new_bottle.name else "this proposal")
        source = f"  [{shown}]" if shown else ""
        also = f"  also: {', '.join(row['also'])}" if row.get("also") else ""
        lines.append(f"  {row['kind']:8s} {row['name']:30s} {row.get('detail', '')}"
                     f"{source}{also}")
    if not lines:
        lines.append("  (no elements)")
    return lines


def _compact_preview(proposal: Proposal, rows: list[dict[str, object]]) -> list[str]:
    """The proposal's own rows in full, then one line per inherited bottle
    counting what it brings, by kind."""
    own = (f"{proposal.new_bottle.name} (new)" if proposal.new_bottle and proposal.new_bottle.name
           else "this proposal")
    lines: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        source = str(row.get("source") or "")
        if source == "_suggest":
            lines.append(f"  {row['kind']!s:8s} {row['name']!s:30s} "
                         f"{row.get('detail', '')}  [{own}]")
        else:
            kinds = counts.setdefault(source, {})
            kinds[str(row["kind"])] = kinds.get(str(row["kind"]), 0) + 1
    for source, kinds in counts.items():
        summary = ", ".join(f"{n} {kind}" for kind, n in kinds.items())
        lines.append(f"  {source}: {summary}")
    return lines or ["  (no elements)"]


__all__ = ["added_counts", "counted", "proposal_elements", "render_proposal",
           "summary_line"]
