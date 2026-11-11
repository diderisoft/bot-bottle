# Decision records

Short, durable records of decisions — one file per decision. This is a
lightweight [Architecture Decision Record](https://adr.github.io/)
practice: capture *what was decided and why* in a versioned file so the
reasoning lives in the clone, not in an issue thread or a chat log
that disappears when the host does.

Decision records and [`docs/reference/`](../reference/architecture.md)
are the public record of the design. PRDs and research notes live in
the private design repo (ADR 0010); a record here must stand without
them. See [`docs/about-these-docs.md`](../about-these-docs.md) for when to write a decision
record.

## Format

One Markdown file per decision, numbered sequentially and zero-padded
(`0001-…`, `0002-…`). Keep it short —
the discipline is writing it down, not the ceremony.

```markdown
# ADR 0000: <short imperative title>

- **Status:** Proposed | Accepted | Superseded by ADR NNNN
- **Date:** YYYY-MM-DD
- **Deciders:** <who>

## Context
What forced the decision; the constraints in play.

## Decision
What we decided, stated plainly.

## Consequences
What follows — the good, and the costs/trade-offs accepted.

## Links
Other decision records, reference pages, issues/PRs. PRDs and research
notes are named in plain text (they're in the private design repo).
Links are convenience pointers; the reasoning above must stand without
them.
```

The records are the index: `ls docs/decisions/` or skim the titles.
No hand-maintained list to keep in sync.
