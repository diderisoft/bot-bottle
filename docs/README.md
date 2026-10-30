# Docs

Start with [`reference/`](reference/). It describes the system as it is now
and is kept current. [`decisions/`](decisions/) records why it is this way.
Together they are the public record of the design; this repository has to
make sense with nothing else.

## Reference: the current system

Edited in place whenever the system changes. A change that alters what a
page describes updates it in the same pull request.

| Page | Covers |
|---|---|
| [architecture.md](reference/architecture.md) | components, layers, code map, launch, attribution, trust, state |
| [backends.md](reference/backends.md) | macos-container, firecracker, docker and their isolation |
| [install.md](reference/install.md) | the installer, host requirements per backend, uninstall |
| [manifest.md](reference/manifest.md) | bottle and agent files, trust, versions, egress route fields |
| [gateway.md](reference/gateway.md) | egress proxy, DLP, supervise, firehose, CA |
| [git-gate.md](reference/git-gate.md) | the git push and fetch gate |
| [host-controller.md](reference/host-controller.md) | jobs, sessions, audit journal, remote access |
| [runtime-contract.md](reference/runtime-contract.md) | routes, roles, tokens and env between host and services |
| [glossary.md](reference/glossary.md) | canonical terms |
| [ci.md](reference/ci.md) | CI workflows and gates |
| [image-build-inputs.md](reference/image-build-inputs.md) | what the infra image hashes cover |

## Decisions: why it is this way

| Artifact | For |
|---|---|
| **Decision record** ([`decisions/`](decisions/)) | A policy, a convention, a boundary, a "we will / won't do this," or a load-bearing choice inside a larger feature that a reader of this repository needs to understand. |
| **Design workflow** ([`design-workflow.md`](design-workflow.md)) | How discussion becomes canonical design, how dependencies are recorded, and when implementation may begin. |

Reference pages say what is true; they don't argue for it. When a page
needs a reason, link the decision record that holds it.

## PRDs and research notes

Feature PRDs and research notes live in the private
`diderisoft/bot-bottle-design` repository, for those who have access
([ADR 0010](decisions/0010-prds-and-research-are-private.md)). Docs and
code here may mention one by number or name, such as "PRD 0081 (design
repo)", but never link into it, and never depend on it: when a choice in a
PRD or a finding in a research note matters to a reader here, write it into
a reference page or a decision record.
