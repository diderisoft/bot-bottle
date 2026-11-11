# ADR 0010: PRDs and research notes live in a private design repo

- **Status:** Accepted
- **Date:** 2026-10-29
- **Deciders:** didericis

## Context

Until now the repository held four kinds of design document: reference pages
(`docs/reference/`), decision records (`docs/decisions/`), feature PRDs
(`docs/prds/`) and research notes (`docs/research/`). The PRDs and research
notes are product strategy as much as design: drafts, rejected options,
competitive analysis, plans that never shipped. They aren't something the
public repository should publish at launch, and a reader of the code doesn't
need them once the reference pages describe the current system.

Reference pages and decision records are different. They are the record of
how the system works and why, and anyone reviewing a security tool needs them.

## Decision

- PRDs and research notes move to the private GitHub repository
  `diderisoft/bot-bottle-design`, with their git history and paths
  (`docs/prds/`, `docs/research/`) preserved. Their conventions (numbering,
  `Status:` lifecycle) live in that repo's `docs/prds/README.md` and
  `docs/research/README.md`.
- `docs/reference/` and `docs/decisions/` stay public. They are where design
  rationale lives from now on.
- The public repository must make sense without the design repo. Docs and code
  here may mention a PRD or research note by number or name, as plain text
  ("PRD 0081 (design repo)"), but never link into it. When a choice made in a
  PRD, or a finding in a research note, matters to a reader here, it goes into
  a reference page or a decision record.
- The PRD-number CI check (`prd-number-check`) and its script leave this
  repository with the PRDs.

## Consequences

- Contributors without access to the design repo see the current design and
  its load-bearing decisions, not the drafts and alternatives behind them.
- Existing references to PRDs in code comments and docs become plain-text
  mentions. Where one carried the only explanation of a behaviour, that
  explanation belongs in a reference page; this is caught over time, not all
  at once.
- PRD numbering is no longer checked by CI; the design repo checks it by hand.
- Removing `docs/prds/` and `docs/research/` from this repository's git history
  is a separate step, done at the public cutover.

## Links

- Issue #612 (move PRDs out of the public tree).
- [`docs/about-these-docs.md`](../about-these-docs.md) — which document to write.
