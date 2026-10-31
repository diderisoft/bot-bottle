"""Render a Proposal as human-readable output (PRD 0117).

Reuses `bottle_elements` from `control/hostctl/manifest_elements` for the
live-like elements preview.  When bottle dirs are not available (e.g. in
tests) the preview falls back to a plain text listing of the proposal fields.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from ..contract.yaml_subset import serialize_yaml_subset
from .proposal import Proposal
from .validate import proposal_to_frontmatter


def render_proposal(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]] | None = None,
    *,
    json_output: bool = False,
) -> str:
    """Return the proposal as a human-readable string.

    When `json_output` is True, returns the raw JSON.  Otherwise prints the
    option, bottles, additions and notes, followed by the `bottle_elements`
    preview if `dirs` is given.
    """
    if json_output:
        return proposal.to_json()
    lines: list[str] = []
    lines.append(f"option:  {proposal.option}")
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
    if dirs is not None:
        lines.append("")
        lines.append("elements preview:")
        lines.extend(_elements_preview(proposal, dirs))
    return "\n".join(lines)


def _elements_preview(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]],
) -> list[str]:
    """Run `bottle_elements` on the proposal's bottles + additions, return
    formatted lines.  Falls back to a short summary on import errors (e.g.
    hostctl not available in the CLI sandbox)."""
    try:
        from ..control.hostctl.manifest_elements import bottle_elements, _chain_files
    except ImportError:
        return ["  (preview unavailable outside host controller)"]

    fm = proposal_to_frontmatter(proposal)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        bottles_dir = tmp_dir / "bottles"
        bottles_dir.mkdir()
        if fm:
            text = f"---\n{serialize_yaml_subset(fm)}---\n\nSuggested bottle.\n"
            (bottles_dir / "_suggest.md").write_text(text)
            tmp_dirs: list[tuple[Path, bool]] = [(bottles_dir, False)] + list(dirs)
            bottle_name = "_suggest"
        else:
            tmp_dirs = list(dirs)
            bottle_name = proposal.bottles[0] if proposal.bottles else "basic"
        try:
            chain = _chain_files([bottle_name], tmp_dirs)
            rows = bottle_elements([bottle_name], tmp_dirs)
        except Exception:  # pylint: disable=broad-exception-caught
            return ["  (could not load bottle chain for preview)"]

    lines: list[str] = []
    for row in rows:
        source = f"  [{row['source']}]" if row.get("source") else ""
        also = f"  also: {', '.join(row['also'])}" if row.get("also") else ""
        lines.append(f"  {row['kind']:8s} {row['name']:30s} {row.get('detail', '')}"
                     f"{source}{also}")
    if not lines:
        lines.append("  (no elements)")
    return lines


__all__ = ["render_proposal"]
