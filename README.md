<p align="center">
  <img src="docs/bottle-glass.svg" alt="bot-bottle" width="140">
</p>

# bot-bottle

Run coding agents unsupervised without letting them wreck your machine or leak your secrets.

bot-bottle runs each agent (Claude Code, Codex or pi) in its own microVM or container. Everything the agent sends out goes through a gateway that holds your credentials and only lets through what the agent's bottle allows.

> **Alpha.** bot-bottle works, and I run my own agents in it every day, but the bottle format and the CLI may still change between releases, and the security design hasn't had an independent review. Please report problems, and anything that gets out of a bottle privately: see [SECURITY.md](SECURITY.md).

[![Demo: bot-bottle drafts a bottle for the task, the agent fixes a failing test, and a blocked host is approved from a phone](https://bot-bottle.com/assets/demo-advanced.jpg)](https://bot-bottle.com/#try)

[Watch the demo](https://bot-bottle.com/#try): a bottle drafted for the task, the agent fixing a failing test, and a blocked host approved from a phone.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

You need git, tmux 3.1+ and a backend: Firecracker on Linux with KVM, Apple's `container` CLI on an Apple silicon Mac, or Docker anywhere else. The installer sets up the host and creates an agent for each harness you're logged in to.

Other install methods and requirements: [install.md](docs/reference/install.md).

## First run

1. In your project, run `bot-bottle start`.
2. Pick an agent.
3. Describe the task: "fix the flaky test in tests/net".
4. Review the bottle it proposes, with only the hosts, repos and secrets the task needs. Press Enter.

The agent starts on your task. If it needs something its bottle doesn't allow, it asks you and waits.

## What's protected

- **Route credentials stay outside the agent.** The gateway injects them on the way out. The agent sees placeholders. → [gateway](docs/reference/gateway.md)
- **Every outbound request is checked against the bottle's allowlist.** Requests on inspected routes are also scanned for secrets. → [gateway](docs/reference/gateway.md#outbound-dlp)
- **Every push through git-gate is secret-scanned** and carries a `Bot-Bottle:` trailer naming its bottle. → [git-gate](docs/reference/git-gate.md#commit-trailers)
- **Each agent runs in its own microVM or container**, checked at launch to have no way out but the gateway. → [backends](docs/reference/backends.md)
- **A repo can't hand you a bottle you haven't trusted.** Any change asks again. → [projects](docs/reference/projects.md)

What it doesn't defend against is in the [threat model](https://bot-bottle.com/docs/security/threat-model).

## Learn more

- [Getting started](https://bot-bottle.com/docs/start): install, first agent, first bottle
- [Threat model](https://bot-bottle.com/docs/security/threat-model): what's in and out of scope
- [All docs](https://bot-bottle.com/docs)
- In this repo: [architecture](docs/reference/architecture.md), [manifest](docs/reference/manifest.md), [gateway](docs/reference/gateway.md), [git-gate](docs/reference/git-gate.md), [backends](docs/reference/backends.md), [install](docs/reference/install.md), [architect](docs/reference/architect.md), [CI](docs/reference/ci.md), [decisions](docs/decisions/README.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AI_POLICY.md](AI_POLICY.md).

## Trademarks

bot-bottle is independent and not affiliated with or endorsed by Anthropic, OpenAI, or the other projects whose agents it runs. Claude, Claude Code and Codex are their owners' trademarks, named here only to say what bot-bottle works with.

## License

Copyright 2026 Eric Bauerfeld. Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
