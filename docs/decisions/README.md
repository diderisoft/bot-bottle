# Decision records

Short, durable records of decisions — one file per decision. This is a
lightweight [Architecture Decision Record](https://adr.github.io/)
practice: capture *what was decided and why* in a versioned file so the
reasoning lives in the clone, not in a Gitea issue thread or a chat log
that disappears when the host does.

See `docs/research/issue-tracking-vs-in-repo-decision-history.md` for
the rationale behind keeping decision history in-repo, and
[`docs/README.md`](../README.md) for when to write a decision record
vs. a PRD or research note.

## Format

One Markdown file per decision, numbered sequentially and zero-padded
(`0001-…`, `0002-…`), matching the PRD numbering style. Keep it short —
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
PRDs, research notes, issues/PRs. Gitea links are convenience
pointers; the reasoning above must stand without them.
```

The records are the index: `ls docs/decisions/` or skim the titles.
No hand-maintained list to keep in sync.
