<p align="center">
  <img src="docs/bottle-glass.svg" alt="bot-bottle" width="140">
</p>

# bot-bottle

Run coding agents unsupervised without letting them wreck your machine or leak your secrets.

## What you get

- **Credentials stay outside the agent.** The gateway injects tokens on the way out; the agent only sees placeholders. → [gateway](docs/reference/gateway.md)
- **Every outbound request is allowlisted and DLP-scanned**, per bottle, with risky matches held for your approval. → [gateway](docs/reference/gateway.md#outbound-dlp)
- **Every push is secret-scanned and traceable to its bottle** through git-gate and `Bot-Bottle:` commit trailers. → [git-gate](docs/reference/git-gate.md#commit-trailers)
- **Each agent runs in its own microVM or container**, checked at launch to have no way out but the gateway. → [backends](docs/reference/backends.md)
- **A repo can't hand you a bottle you haven't trusted**; any change asks again. → [manifest](docs/reference/manifest.md#trust)

## Quickstart

```sh
curl -fsSL "https://gitea.dideric.is/api/v1/repos/didericis/bot-bottle/raw/install.sh?ref=main" | sh
```

or, with uv (or the same with `pipx install`):

```sh
uv tool install "bot-bottle[remote] @ git+https://gitea.dideric.is/didericis/bot-bottle.git"
```

The installer runs `bot-bottle setup`, which writes a starter agent for each logged-in harness it finds (Claude Code, Codex, pi). Start one; images are pulled or built on first run:

```sh
bot-bottle start claude
```

You also need a backend, which the installer leaves to you (`bot-bottle doctor` says what's missing):

- **macOS:** Apple's `container` CLI, plus a one-time host firewall (`bot-bottle install --backend=macos-container` prints the `sudo` lines).
- **Linux:** Firecracker with KVM, plus a one-time network pool (`bot-bottle install --backend=firecracker`).
- **Anywhere else:** Docker, with `--backend=docker`.

Details, including Python and tmux requirements, are in [install.md](docs/reference/install.md). To remove it all: `bot-bottle uninstall`.

## Your first bottle

A bottle says what an agent may reach. `~/.bot-bottle/bottles/web.md`:

```markdown
---
extends: basic              # package registries, downloads only
egress:
  routes:
    - host: api.example.com
      inspect:
        auth: { scheme: Bearer, token_ref: EXAMPLE_API_TOKEN }   # injected; the agent never sees it
---
```

Point an agent at it with `bottle: web` in `~/.bot-bottle/agents/<name>.md`. Every field is in [manifest.md](docs/reference/manifest.md).

## Learn more

- [Architecture](docs/reference/architecture.md): how the pieces fit
- [Manifest](docs/reference/manifest.md): bottles, agents, trust, egress routes
- [Gateway](docs/reference/gateway.md): egress proxy, DLP, supervise
- [git-gate](docs/reference/git-gate.md): scanned pushes and commit trailers
- [Backends](docs/reference/backends.md): macos-container, firecracker, docker
- [Install](docs/reference/install.md): requirements, host setup, uninstall
- [CI](docs/reference/ci.md): workflows and gates
- [Decisions](docs/decisions/README.md): why it is this way

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AI_POLICY.md](AI_POLICY.md).

## Trademarks

bot-bottle is independent and not affiliated with or endorsed by Anthropic, OpenAI, or the other projects whose agents it runs. Claude, Claude Code and Codex are their owners' trademarks, named here only to say what bot-bottle works with.

## License

Copyright 2026 Eric Bauerfeld. Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
