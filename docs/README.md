# bot-bottle docs

bot-bottle runs coding agents with broad permissions inside an isolated
microVM or container, so a misbehaving agent can't reach your machine. It runs
Claude Code, Codex and pi.

Credentials stay outside the agent. A gateway adds them on the way out. What
leaves is checked:

- every request, against the bottle's allowlist
- requests on inspected routes, for secrets
- every push, through a scanned git gate

## Start here

[Getting started](start.md): requirements, the one-line install, your first
agent, and your first bottle.

## Concepts

| Term | What it is | Reference |
|---|---|---|
| **Bottle** | A file that says what an agent may reach: hosts, repos, env and the credentials each route carries. | [manifest.md](reference/manifest.md#bottles) |
| **Agent** | A file that says who the agent is: its harness, skills and prompt. It names no bottle. You pick one at launch. | [manifest.md](reference/manifest.md#agents) |
| **Gateway** | The one way out of a bottle. It enforces the allowlist, injects tokens, scans inspected requests for secrets, and asks you about what the bottle doesn't allow. | [gateway.md](reference/gateway.md) |
| **git-gate** | The gateway's git server. Pushes are secret-scanned and carry a trailer naming the bottle that made them. | [git-gate.md](reference/git-gate.md) |
| **Secret** | A value kept on the host, in the secrets file or your harness's login. Bottles name secrets, never hold them. The gateway injects most of them. | [secrets.md](reference/secrets.md) |
| **Trust** | A bottle or agent shipped in a repo runs only after this host trusts its exact contents. Any change asks again. | [manifest.md](reference/manifest.md#trust) |
| **Console** | An optional web app, paired with your host, for watching agents and answering their requests from your phone. | [host-controller.md](reference/host-controller.md#remote-access) |

Every term is defined in the [glossary](reference/glossary.md).

## Security

| Page | Covers |
|---|---|
| [Threat model](security/threat-model.md) | what bot-bottle defends against, and what it doesn't |
| [Escape attempts](security/escape-attempts.md) | sandbox escape and exfiltration techniques tried against bot-bottle, each with the test that catches it |

Found a way out? Report it privately through
[GitHub's vulnerability reporting](https://github.com/diderisoft/bot-bottle/security/advisories/new).
What's in scope is in [SECURITY.md](../SECURITY.md).

## Reference

How the system works now.

| Page | Covers |
|---|---|
| [architecture.md](reference/architecture.md) | components, layers, code map, launch, attribution, trust, state |
| [manifest.md](reference/manifest.md) | bottle and agent files, trust, versions, egress route fields |
| [bottles-suggest.md](reference/bottles-suggest.md) | the guided `bot-bottle start` and the bottle it proposes |
| [gateway.md](reference/gateway.md) | egress proxy, DLP, supervise, firehose, CA |
| [git-gate.md](reference/git-gate.md) | the git push and fetch gate, forges, commit trailers |
| [secrets.md](reference/secrets.md) | the secrets file, how bottles name secrets, who holds each value |
| [backends.md](reference/backends.md) | macos-container, firecracker, docker and their isolation |
| [install.md](reference/install.md) | the installer, host requirements per backend, uninstall |
| [host-controller.md](reference/host-controller.md) | jobs, sessions, audit journal, remote access |
| [glossary.md](reference/glossary.md) | canonical terms |

### For contributors

| Page | Covers |
|---|---|
| [runtime-contract.md](reference/runtime-contract.md) | routes, roles, tokens and env between host and services |
| [ci.md](reference/ci.md) | CI workflows and gates |
| [image-build-inputs.md](reference/image-build-inputs.md) | what the infra image hashes cover |
| [decisions/](decisions/README.md) | decision records |
| [design-workflow.md](design-workflow.md) | how discussion becomes design, and when implementation may begin |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | commit format, pull requests |

## Why it's this way

[Decision records](decisions/README.md): short records of the policies,
boundaries and load-bearing choices behind the design.

## About these docs

| Kind | Says | Kept |
|---|---|---|
| Reference pages | what is true now, linking the decision record that says why | Edited in place. A change that alters what a page describes updates it in the same pull request. |
| Decision records | why: one per policy, convention, boundary or load-bearing choice a reader of this repository needs to understand | ADR-lite, numbered `NNNN-kebab.md`. |
| PRDs and research notes | feature design and research | In a private design repository. |

Docs here may mention a PRD by number, but never depend on it. When a choice
in one matters to a reader, it is written into a reference page or a decision
record. This repository makes sense with nothing else.

### Writing these docs

- One idea per sentence. No semicolons. At most one parenthetical per
  paragraph.
- Paragraphs of 1-3 sentences, under about 50 words. Anything longer becomes
  a list or a table.
- Lead each section with the answer in one sentence, then the detail. Put the
  example (command, YAML, table) before the explanation.
- Use tables for comparisons and "which X does what". Use short lists for
  steps and limits. No bullet runs past 3 lines.
- Cut meta and hedging, such as "This page describes…", "Note that…" or "in
  practice".
- Name internals only where a reader verifying a security claim needs them,
  and only once.
- Keep every fact. Detail too fine for the main flow moves to a "Details"
  subsection.
