<p align="center">
  <img src="docs/logo.svg" alt="bot-bottle logo" width="140">
</p>

# bot-bottle

[![test](https://gitea.dideric.is/didericis/bot-bottle/actions/workflows/test.yml/badge.svg?branch=main)](https://gitea.dideric.is/didericis/bot-bottle/actions?workflow=test.yml)
[![pylint](https://img.shields.io/badge/pylint-9.92%2F10-brightgreen)](https://github.com/PyCQA/pylint)
[![pyright](https://img.shields.io/badge/pyright-18%20errors-brightgreen)](https://github.com/microsoft/pyright)

**Problem:** Developer wants to run a coding agent without supervision, but they don't want a prompt injected or misbehaving agent wrecking their environment or exfiltrating sensitive data.

**Solution:** Ephemeral, per agent "bottles" the agent cannot modify that scan all traffic for data exfiltration and limit capabilities and egress to only what the agent needs.

## Features

- **Per-bottle egress allowlist** — TLS-bumped HTTP/HTTPS chokepoint with a per-manifest host allowlist and request-body DLP scanner; DoH and arbitrary hosts blocked by default.
- **Tokens the agent never sees** — host secrets live in a sidecar; the agent dials `http://sidecar:9099/<path>` and the proxy strips inbound `Authorization` and injects the real token before forwarding. `printenv` in the agent shows proxy URLs only.
- **Gitleaks-scanned push (git-gate)** — `bottle.git` remotes route through a per-bottle `git daemon` that gitleaks-scans incoming refs pre-receive and forwards clean refs upstream over SSH. The agent never holds the upstream credential.
- **Manifest-scoped skills + secrets** — each bottle declares its skills, env, git identity, remotes, and egress routes; unknown keys die at load.
- **Trust boundary at `$HOME`** — bottles (credentials, egress, remotes) live only under `~/.bot-bottle/bottles/`. Repos may ship agents but not bottles, so a cloned repo can't redirect an env var to an attacker host.
- **Composable bottles (`extends:`)** — keep provider/runtime policy in one base bottle (e.g. `claude.md`) and overlay task bottles on top.
- **Parallel, isolated bottles** — each bottle is its own per-agent Docker `--internal` network; bottles don't share state or talk to each other.
- **Provider templates (Claude, Codex)** — `Dockerfile.claude` / `Dockerfile.codex`, or a bottle-supplied Dockerfile. Claude auth via long-lived OAuth token; Codex via opt-in host device-auth forwarding.
- **gVisor auto-detect** — on Linux hosts where `runsc` is registered with Docker, every bottle launches under it for a userspace syscall barrier; no manifest config required.
- **Smolmachines backend (macOS)** — opt-in `BOT_BOTTLE_BACKEND=smolmachines` runs the agent in a libkrun micro-VM with the sidecar bundle still in Docker.

## Architecture

A bottle is two containers per agent: an `agent` container, and a `sidecars` container that bundles pipelock + cred-proxy + git-gate + supervise behind a Python init supervisor. They share a per-agent Docker `--internal` network; the agent has no default route off-box.

```
                            host  ( ./cli.py )
                                  │
                          starts  │  stops
                                  ▼
   ┌─────────────────────────── bottle ──────────────────────────────────┐
   │                                                                     │
   │   ┌──────────────────┐                   ┌──────────────┐           │
   │   │ agent image      │   HTTP(S) proxy   │ cred-proxy   │           │
   │   │ (claude-code,    │ ─────────────────►│ (strips/inj  │           │
   │   │  codex, etc)     │                   │  Authoriz.)  │           │
   │   │                  │                   └──────┬───────┘           │
   │   │ environ: URLs    │                          │                   │
   │   │ only, no real    │                          ▼                   │
   │   │ tokens           │                  ┌────────────────┐          │  HTTPS to
   │   │                  │                  │ pipelock image │──────────┼──►  allowlisted
   │   │                  │                  │ (TLS bump, DLP │          │     hosts (incl.
   │   │                  │                  │  body scan,    │          │      cred-proxy
   │   │                  │                  │  allowlist)    │          │      upstreams)
   │   │                  │                  └────────────────┘          │
   │   │                  │                                              │
   │   │                  │    git proxy     ┌────────────────┐          │  SSH push/fetch
   │   │                  │ ────────────────►│ git-gate image │──────────┼──►  to bottle.git
   │   │                  │                  │ (gitleaks +    │          │      upstreams
   │   └──────────────────┘                  │  git daemon)   │          │     (direct — not
   │                                         └────────────────┘          │      via pipelock)
   │                                                                     │
   │   agent on internal network (no default route); pipelock,           │
   │   cred-proxy, and git-gate straddle internal + egress networks.     │
   │   pipelock is the single HTTP/HTTPS chokepoint — cred-proxy's       │
   │   outbound traverses it too. git-gate's SSH egress is direct        │
   │   because pipelock is HTTP-only.                                    │
   └─────────────────────────────────────────────────────────────────────┘
```

When the agent exits, `cli.py` tears down every sidecar and both networks; nothing about a bottle persists between runs.

## Quickstart

Requires Docker on the host and a long-lived Claude Code OAuth token (`claude setup-token`) exported as `BOT_BOTTLE_CLAUDE_OAUTH_TOKEN`.

```sh
./cli.py start <agent>   # builds the image on first run, drops you into claude
```

## Manifest

Bottles and agents are Markdown files with YAML frontmatter under `~/.bot-bottle/`. The Markdown body is the system prompt. Bottles live in `~/.bot-bottle/bottles/`; agents may also be shipped by a repo at `<repo>/.bot-bottle/agents/<name>.md`.

**Bottle** (`~/.bot-bottle/bottles/gitea-dev.md`):

````markdown
---
extends: claude          # inherit the Claude provider boundary

env:
  GIT_AUTHOR_NAME: didericis

git:
  user:
    name: "Eric Bauerfeld"
    email: "eric+claude@dideric.is"
  remotes:
    gitea.dideric.is:
      Name: bot-bottle
      Upstream: ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git
      IdentityFile: /Users/didericis/.ssh/id_ed25519_gitea
      KnownHostKey: ssh-ed25519 AAAA...

egress:
  routes:
    - host: gitea.dideric.is
      auth:
        scheme: token
        token_ref: BOT_BOTTLE_GITEA_TOKEN
      pipelock:
        ssrf_ip_allowlist: [192.0.2.10/32]
---

The `gitea-dev` bottle. Provider auth via the inherited Claude route;
gitea over SSH for push, token over HTTPS for the API.
````

**Agent** (`~/.bot-bottle/agents/gitea-helper.md`):

````markdown
---
bottle: gitea-dev
skills:
  - init-prd
---

You help maintain Gitea-hosted projects.
````

More examples in `examples/`. Full design lives under `docs/prds/`; the trust-boundary rationale is in `docs/prds/0011-per-file-md-manifest.md`.

## Trademarks

bot-bottle is an independent project and is not affiliated with, endorsed by, or sponsored by Anthropic, PBC. "Claude" and "Claude Code" are trademarks of Anthropic, PBC; the project name uses "claude" descriptively to indicate that the tool runs Claude Code inside a sandbox.

## License

Copyright 2026 Eric Bauerfeld. Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
