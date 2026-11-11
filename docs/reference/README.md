# Reference

How the system works now.

| Page | Covers |
|---|---|
| [architecture.md](architecture.md) | components, layers, code map, launch, attribution, state |
| [manifest.md](manifest.md) | bottle and agent files, versions, egress route fields |
| [architect.md](architect.md) | the bottle architect: the guided `bot-bottle start`, what it proposes and how it's checked |
| [gateway.md](gateway.md) | egress proxy, DLP, supervise, firehose, CA |
| [git-gate.md](git-gate.md) | the git push and fetch gate, forges, commit trailers |
| [secrets.md](secrets.md) | the secrets file, how bottles name secrets, who holds each value |
| [projects.md](projects.md) | a project's bottles and agents, when they need trust, and what trust records |
| [backends.md](backends.md) | macos-container, firecracker, docker and their isolation |
| [install.md](install.md) | the installer, host requirements per backend, uninstall |
| [host-controller.md](host-controller.md) | jobs, sessions, audit journal, remote access |
| [glossary.md](glossary.md) | canonical terms |

## For contributors

| Page | Covers |
|---|---|
| [runtime-contract.md](runtime-contract.md) | routes, roles, tokens and env between host and services |
| [ci.md](ci.md) | CI workflows and gates |
| [image-build-inputs.md](image-build-inputs.md) | what the infra image hashes cover |
| [decisions/](../decisions/README.md) | decision records |
| [design-workflow.md](../design-workflow.md) | how discussion becomes design, and when implementation may begin |
| [about-these-docs.md](../about-these-docs.md) | which document to write, and the house style |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | commit format, pull requests |
