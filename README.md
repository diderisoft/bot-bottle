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
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

That installs the latest release. Or, with uv (or the same with `pipx install`), pinning the latest `vX.Y.Z` tag:

```sh
uv tool install "bot-bottle[remote] @ git+https://github.com/diderisoft/bot-bottle.git@v0.1.6"
```

The installer runs `bot-bottle bootstrap`: it sets up this host's backend (asking before the one step that needs `sudo`), writes a starter agent for each logged-in harness it finds (Claude Code, Codex, pi), and checks it all. Then start, and say what the agent should do; the release's images are pulled on first run:

```sh
bot-bottle start
```

It asks which harness (once; it remembers), asks for the task, and proposes a bottle sized to it: the hosts, repos and env it needs and no more, which you can run, edit, or save. A bare `bot-bottle` opens a home menu: start an agent, attach to a running one, answer approvals, check the host, or set up agents. `bot-bottle start --auto "fix the flaky test"` runs the proposal without asking. Details in [bottles-suggest.md](docs/reference/bottles-suggest.md).

You also need a backend, which the installer leaves to you (`bot-bottle doctor` says what's missing):

- **macOS:** Apple's `container` CLI, plus a one-time host firewall (`bot-bottle install --backend=macos-container` prints the `sudo` lines).
- **Linux:** Firecracker with KVM, plus a one-time network pool (`bot-bottle install --backend=firecracker`).
- **Anywhere else:** Docker, with `--backend=docker`.

Running from `main` instead of a release (`sh -s -- --main` on the installer)? Its orchestrator and gateway images aren't published: run `bot-bottle up --build` first ([install](docs/reference/install.md#releases-and-infra-images)).

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
        auth: { type: bearer, value: secret:EXAMPLE_API_TOKEN }   # injected; the agent never sees it
---
```

Run an agent in it with `bot-bottle start claude --bottle web`; agents don't name a bottle, and a plain start suggests one (or runs in the built-in `basic` when you give no task). Every field is in [manifest.md](docs/reference/manifest.md).

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
