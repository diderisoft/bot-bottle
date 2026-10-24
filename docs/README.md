# Docs

Start with [`reference/`](reference/). It describes the system as it is now
and is kept current. The rest is history: what was proposed, investigated and
decided, and why.

## Reference: the current system

Edited in place whenever the system changes. A shipped PRD updates the pages
it affects.

| Page | Covers |
|---|---|
| [architecture.md](reference/architecture.md) | components, layers, code map, launch, attribution, trust, state |
| [backends.md](reference/backends.md) | macos-container, firecracker, docker and their isolation |
| [gateway.md](reference/gateway.md) | egress proxy, DLP, supervise, firehose, CA |
| [git-gate.md](reference/git-gate.md) | the git push and fetch gate |
| [host-controller.md](reference/host-controller.md) | jobs, sessions, audit journal, remote access |
| [runtime-contract.md](reference/runtime-contract.md) | routes, roles, tokens and env between host and services |
| [glossary.md](reference/glossary.md) | canonical terms |
| [ci.md](reference/ci.md) | CI workflows and gates |
| [image-build-inputs.md](reference/image-build-inputs.md) | what the infra image hashes cover |

## History: why it is this way

| Artifact | For |
|---|---|
| **PRD** ([`prds/`](prds/)) | A feature: what to build, scope, success criteria. Superseded and retargeted PRDs move to [`prds/archive/`](prds/archive/). |
| **Research note** ([`research/`](research/)) | A landscape or tradeoff investigation. |
| **Decision record** ([`decisions/`](decisions/)) | A decision that isn't itself a feature: a policy, a convention, a "we will / won't do this," or a load-bearing choice inside a larger PRD that deserves to be discoverable on its own. |
| **Design workflow** ([`design-workflow.md`](design-workflow.md)) | How discussion becomes canonical design, how dependencies are recorded, and when implementation may begin. |

A decision that's fully specified by a PRD doesn't need duplicating in
a decision record. Write one when the *decision* would otherwise be
buried in prose, lost in an issue thread, or have no in-repo home at
all (small requests that don't merit a PRD; non-feature choices like
merge strategy or a trust posture).

Reference pages say what is true; they don't argue for it. When a page
needs a reason, link the PRD or decision record that holds it.
