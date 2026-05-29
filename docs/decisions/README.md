# Decision records

Short, durable records of decisions — one file per decision. This is a
lightweight [Architecture Decision Record](https://adr.github.io/)
practice: capture *what was decided and why* in a versioned file so the
reasoning lives in the clone, not in a forge issue thread or a chat log
that disappears when the provider does.

See `docs/research/issue-tracking-vs-in-repo-decision-history.md` for
the rationale behind keeping decision history in-repo.

## When to write one

| Artifact | For |
|---|---|
| **PRD** (`docs/prds/`) | A feature: what to build, scope, success criteria. |
| **Research note** (`docs/research/`) | A landscape/tradeoff investigation. |
| **Decision record** (`docs/decisions/`) | A decision that isn't itself a feature — a policy, a convention, a "we will / won't do this," or a load-bearing choice made inside a larger PRD that deserves to be discoverable on its own. |

A decision that's fully specified by a PRD doesn't need duplicating
here. Write a record when the *decision* would otherwise be buried in
prose, lost in an issue thread, or have no in-repo home at all (small
requests that don't merit a PRD; non-feature choices like merge
strategy or a trust posture).

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
PRDs, research notes, issues/PRs. Forge links are convenience
pointers; the reasoning above must stand without them.
```

The records are the index: `ls docs/decisions/` or skim the titles.
No hand-maintained list to keep in sync.
