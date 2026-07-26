# Docs

How this project records what it builds and why — and a guide to
picking the right document for what you're capturing.

## When to write which document

| Artifact | For |
|---|---|
| **Design workflow** (`docs/design-workflow.md`) | How discussion becomes canonical design, how dependencies are recorded, and when implementation may begin. |
| **Glossary** (`docs/glossary.md`) | Canonical term definitions — what words mean in this project. |
| **PRD** (`docs/prds/`) | A feature: what to build, scope, success criteria. |
| **Research note** (`docs/research/`) | A landscape/tradeoff investigation. |
| **Decision record** (`docs/decisions/`) | A decision that isn't itself a feature — a policy, a convention, a "we will / won't do this," or a load-bearing choice made inside a larger PRD that deserves to be discoverable on its own. |

A decision that's fully specified by a PRD doesn't need duplicating in
a decision record. Write one when the *decision* would otherwise be
buried in prose, lost in an issue thread, or have no in-repo home at
all (small requests that don't merit a PRD; non-feature choices like
merge strategy or a trust posture).
