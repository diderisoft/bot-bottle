<p align="center">
  <img src="docs/logo.svg" alt="bot-bottle logo" width="140">
</p>

# bot-bottle

[![test](https://gitea.dideric.is/didericis/bot-bottle/actions/workflows/test.yml/badge.svg?branch=main)](https://gitea.dideric.is/didericis/bot-bottle/actions?workflow=test.yml)
[![coverage](https://img.shields.io/badge/coverage-85%25-brightgreen)](https://coverage.readthedocs.io/)
[![core coverage](https://img.shields.io/badge/core%20coverage-94%25-brightgreen)](https://gitea.dideric.is/didericis/bot-bottle/src/branch/main/docs/decisions/0004-coverage-policy.md)

**Problem:** Developer wants to run a coding agent without supervision, but they don't want a prompt injected or misbehaving agent wrecking their environment or exfiltrating sensitive data.

**Solution:** Ephemeral, per agent "bottles" the agent cannot modify that scan all traffic for data exfiltration and limit capabilities and egress to only what the agent needs.

## Features

- **Per-bottle egress allowlist** — TLS-bumped HTTP/HTTPS chokepoint with a per-manifest host allowlist; per-route path/method/header `matches` filtering; outbound DLP scanning for known tokens and secrets, inbound DLP scanning for prompt-injection attempts; DoH and arbitrary hosts blocked by default.
- **Per-route token-match policy** — each egress route picks what happens when the outbound DLP catches a token via `dlp.outbound_on_match`: `supervise` (default) holds the request and surfaces it in `bot-bottle supervise` for approval (an approved value is remembered for the life of the proxy); `redact` scrubs the value and forwards; `block` is a hard `403`. Cuts false-positive friction without weakening default-deny.
- **Tokens the agent never sees** — host secrets live in a gateway; the agent dials `http://gateway:9099/<path>` and the proxy strips inbound `Authorization` and injects the real token before forwarding. `printenv` in the agent shows proxy URLs only.
- **Gitleaks-scanned push (git-gate)** — `bottle.git` remotes route through the gateway's git-gate, which gitleaks-scans incoming refs pre-receive, refuses any push carrying a secret the bottle holds (its `?` secret env values and its exfil canary, matched by value rather than by shape), and forwards clean refs upstream over SSH. The agent never holds the upstream credential.
- **Manifest-scoped skills + secrets** — each bottle declares its skills, env, git identity, remotes, and egress routes; unknown keys die at load.
- **Trust boundary at `$HOME`** — bottles (credentials, egress, remotes) live only under `~/.bot-bottle/bottles/`. Repos may ship agents but not bottles, so a cloned repo can't redirect an env var to an attacker host.
- **Composable bottles (`extends:`)** — keep provider/runtime policy in one base bottle (e.g. `claude.md`) and overlay task bottles on top.
- **Parallel, isolated bottles** — each bottle runs in its own backend-owned isolation boundary; bottles don't share state. They share the per-host gateway, which attributes every request to its bottle by source IP and keeps each bottle's policy, tokens and git-gate repos separate.
- **Provider templates (Claude, Codex)** — `bot_bottle/contrib/{claude,codex,pi}/Dockerfile`, or a bottle-supplied Dockerfile. Claude auth via long-lived OAuth token; Codex via opt-in host device-auth forwarding.
- **gVisor auto-detect** — on Linux hosts where `runsc` is registered with Docker, every bottle launches under it for a userspace syscall barrier; no manifest config required.
- **Apple Container backend (macOS default when available)** — runs the agent and gateway with Apple's `container` CLI, using a host-only agent network plus a separate gateway egress network.
- **Firecracker backend (Linux default when available)** — runs the agent in a KVM Firecracker microVM reached over SSH on a point-to-point TAP; the orchestrator and gateway run in their own per-host microVMs, so the host needs no Docker. A dedicated, fail-closed `nftables` table isolates the guest. Requires KVM (`/dev/kvm`) and a one-time privileged network-pool setup.
- **Docker backend (fallback)** — for CI and hosts without Apple Container or KVM, via `BOT_BOTTLE_BACKEND=docker` or `--backend=docker`. It is the weakest boundary: agents are containers sharing the host kernel (gVisor narrows that when installed). Their network is `--internal`, so the gateway is still the only way out.
- **Launch-time isolation probe** — before an agent runs, bot-bottle proves from inside it that it cannot reach the host or a public address without the gateway (and, on Docker, cannot resolve public names), and refuses to start it otherwise. Firecracker and Docker run it today.

## Architecture

Every backend has the same shape. Each host runs one **orchestrator** (the
control plane) and one **gateway** (the data plane), started on demand and
shared by every bottle on that host. Each bottle is just the agent, in its own
container or microVM, pointed at the gateway for all outbound traffic.

- The **gateway** runs the egress proxy (mitmproxy: TLS bumping, route
  allowlist, DLP, token injection), git-gate (a smart-HTTP git server that
  gitleaks-scans pushes and forwards clean refs upstream over SSH) and the
  supervise endpoint. It attributes each request to a bottle by the agent's
  source IP, which the backend pins and the agent cannot change, so one gateway
  enforces each bottle's own policy.
- The **orchestrator** holds the bottle registry, the signing key and the
  approval queue. Agents have no route to it; the gateway and the host CLI do.
- The **host controller** runs on the host as your user. The CLI and the
  orchestrator use it to launch, list, attach to and tear down bottles. Each
  agent session runs in tmux, so it survives the terminal that started it and
  can have several viewers.

```
  host CLI ──► host controller ──► backend (launch / teardown)
                                       │
  ┌──────────── per host ──────────────┼──────────────────────────────────┐
  │                                    ▼                                  │
  │  ┌───────────────┐       ┌─────────────────────────┐                  │
  │  │ agent         │ HTTPS │ gateway                 │ HTTPS to         │
  │  │ (claude,      │──────►│  egress proxy (DLP,     │──► allowlisted   │
  │  │  codex, pi)   │       │   token injection)      │    hosts         │
  │  │               │  git  │  git-gate (gitleaks)    │ SSH push to      │
  │  │ env: proxy    │──────►│  supervise              │──► upstreams     │
  │  │ URLs only,    │       └────────────┬────────────┘                  │
  │  │ no real       │                    │ control network               │
  │  │ tokens        │       ┌────────────▼────────────┐                  │
  │  └───────────────┘       │ orchestrator            │                  │
  │   one per bottle;        │  registry, signing key, │                  │
  │   no route to the        │  approval queue         │                  │
  │   control plane          └─────────────────────────┘                  │
  └───────────────────────────────────────────────────────────────────────┘
```

How each backend builds that boundary:

| Backend | Agent | Orchestrator and gateway | What confines the agent |
|---|---|---|---|
| **macos-container** (macOS default) | Apple container | Two Apple containers | Host-only agent network; only the gateway is also on the NAT egress and control networks |
| **firecracker** (Linux default with KVM) | Firecracker microVM, reached over SSH | Two Firecracker microVMs | Point-to-point TAP per bottle plus a fail-closed `nftables` table (`inet bot_bottle_fc`); the gateway's ports are DNAT'd to the gateway VM, and nothing else leaves the box |
| **docker** (fallback) | Docker container (gVisor `runsc` when registered) | Two Docker containers | `--internal` agent network with no route off the box; only the gateway is also on the egress and control networks |

When the agent exits, its bottle is deregistered from the orchestrator, its
git-gate deploy keys are revoked, and its container or VM is removed. The
orchestrator and gateway keep running for the next bottle.

## Quickstart

```sh
curl -fsSL https://gitea.dideric.is/didericis/bot-bottle/raw/branch/main/install.sh | sh
```

The installer is a bootstrapper: it finds a suitable Python, installs bot-bottle with `pipx` (falling back to `pip --user`), creates `~/.bot-bottle`, and runs `bot-bottle doctor`. It is idempotent and never uses `sudo`. Python-native users can skip it entirely with `pipx install bot-bottle` or `uv tool install bot-bottle`.

### Requirements

**Python ≥ 3.11**, and this is the one that trips people up on macOS: the `python3` Apple ships at `/usr/bin/python3` is **3.9.6**, which is too old. Bare `python3` resolves to that stub far more often than people expect. `path_helper` builds a login shell's `PATH` from `/etc/paths` and then appends `/etc/paths.d/*`, and `/usr/bin` sits in the former — so even when `/opt/homebrew/bin` *is* on the `PATH` (via `/etc/paths.d/homebrew`), it comes after `/usr/bin` and loses. Prepending a newer Python is something your shell profile does, and a fresh account, a launchd job, or a CI runner has no such profile. So the installer looks past bare `python3` before giving up: it tries `python3`, then the versioned `python3.11`–`python3.14` names, then `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, and python.org framework builds — and tells you which one it picked when it isn't the obvious one. Point it somewhere specific with `BOT_BOTTLE_PYTHON=/path/to/python3`.

**No `pipx` required.** If `pipx` is present the installer uses it and stays out of the way. If it isn't, bot-bottle installs into a private venv at `~/.bot-bottle/venv` (override with `BOT_BOTTLE_VENV`) and symlinks the entry point into `~/.local/bin`. There is deliberately no `pip install --user` path: Homebrew, python.org and Debian/Ubuntu interpreters are all externally managed (PEP 668), which blocks `--user` outright — so on a Mac it is never the fallback it appears to be. A venv is exempt from PEP 668, and `venv` is stdlib, so unlike `pipx` there is nothing to bootstrap first.

**`git`**, because the default install spec is a `git+` URL. Set `BOT_BOTTLE_INSTALL_SPEC` to a wheel path or index name to avoid it.

**A backend**, which the installer deliberately does *not* install for you — `doctor` reports what's missing afterwards. On compatible macOS hosts, the default backend requires Apple's `container` CLI and does not require Docker. The Firecracker backend (Linux) requires the `firecracker` binary, KVM and e2fsprogs; Docker is needed only to build the infra images from source instead of using a release. The Docker backend requires Docker. Every backend needs tmux 3.1 or newer, which runs the agent sessions. Claude bottles also need a long-lived Claude Code OAuth token (`claude setup-token`) exported as `BOT_BOTTLE_CLAUDE_OAUTH_TOKEN`.

Use `BOT_BOTTLE_BACKEND=docker bot-bottle start <agent>` on hosts where neither Apple Container nor KVM is available and Docker is the desired backend.

> **CI (macOS Apple Container):** the advisory `integration-macos` job in `.gitea/workflows/pre-release-test.yml` runs only on manual dispatch. It targets a self-hosted host-mode runner labelled `macos`; Apple Container cannot run inside the Linux pull-request runner. Provision an Apple Silicon host with the `container` CLI running and Python ≥ 3.11 plus `coverage` on the launchd service's explicit `PATH`. The infra container is a singleton (`bot-bottle-mac-infra`), so keep runner concurrency at 1. Its coverage is reported separately and never feeds the required pull-request gate.

### Containers inside a bottle

A bottle may set `nested_containers: true`. On the macOS backend this starts a
guest-local, rootless **podman** service after the bottle is registered and
exposes its Docker-compatible API socket, so the agent still runs `docker` and
`docker compose`. Nothing is mounted from the host: Docker Desktop's socket
stays out of the bottle and the guest gains no outer VM capabilities. Backends
that cannot do this (`docker`, `firecracker`) reject the flag rather than
silently ignore it.

Rootless Docker was tried first and does not work here at all: Apple
Container's capability bounding set omits `CAP_SYS_ADMIN`, which the kernel
requires to write a multi-range `uid_map`. See
[`docs/research/rootless-docker-in-apple-container-spike.md`](docs/research/rootless-docker-in-apple-container-spike.md).

The tradeoff to understand before enabling it: podman avoids that requirement
by falling back to a single-UID mapping, so nested containers provide **no
isolation from the agent itself** — `root` inside a nested container is the
agent user outside it. Nested containers are a build/test convenience, not a
security boundary. The bottle remains the boundary.

Pulling images goes through the bottle's egress proxy like every other
request, so each registry needs a route — **and so does the CDN it redirects
layer blobs to**, which is a different host. Without the CDN route the pull
authenticates, fetches the manifest, then 403s partway through.

Docker Hub and GHCR additionally need `preserve_auth: true`: their token dance
uses a client-fetched per-scope bearer token that the proxy would otherwise
strip. Turn DLP off on every registry and CDN route — the bodies are
compressed layer blobs that no detector can read, and buffering them is what
triggers the shared-proxy OOM in #455.

```yaml
nested_containers: true
egress:
  routes:
    # Docker Hub: registry, token endpoint, blob CDN.
    - host: registry-1.docker.io
      preserve_auth: true
      dlp: { outbound_detectors: false, inbound_detectors: false }
    - host: auth.docker.io
      preserve_auth: true
      dlp: { outbound_detectors: false, inbound_detectors: false }
    - host: production.cloudfront.docker.com
      dlp: { outbound_detectors: false, inbound_detectors: false }
    # GHCR: registry + blob CDN.
    - host: ghcr.io
      preserve_auth: true
      dlp: { outbound_detectors: false, inbound_detectors: false }
    - host: pkg-containers.githubusercontent.com
      dlp: { outbound_detectors: false, inbound_detectors: false }
    # quay.io: registry + blob CDNs. No preserve_auth needed for public pulls.
    - host: quay.io
      dlp: { outbound_detectors: false, inbound_detectors: false }
    - host: cdn01.quay.io
      dlp: { outbound_detectors: false, inbound_detectors: false }
    - host: cdn02.quay.io
      dlp: { outbound_detectors: false, inbound_detectors: false }
    - host: cdn03.quay.io
      dlp: { outbound_detectors: false, inbound_detectors: false }
```

`mcr.microsoft.com` and `registry.k8s.io` follow the same shape and also
redirect blobs elsewhere (`*.data.mcr.microsoft.com` and
`us-*-docker.pkg.dev` respectively); route whichever host the 403 names.

Inside a nested container the same allowlist applies: an allowlisted host
returns 200 and anything else gets a 403 straight from the proxy. The
gateway's CA bundle and proxy settings are wired in automatically, so
`docker run … curl https://…` works with no extra flags — no `--add-host`,
`-e`, or `-v`.

Two things worth knowing when testing that:

- Public DNS inside a nested container fails **by design**. Everything
  egresses through the proxy, so `nslookup` failing is expected and is not
  evidence of a problem.
- Alpine's BusyBox `wget` drops the connection after the proxy's TLS
  interception and reports `error getting response`, even though the proxy
  logs the decrypted request and returns a response. Use `curl` to test
  egress; BusyBox `wget` will lie to you.

### Firecracker on Linux

On Linux, a KVM-capable host defaults to the Firecracker backend. It needs:

- **`/dev/kvm`** present and accessible. Load `kvm-intel` or `kvm-amd` (and enable virtualization in BIOS/firmware). The invoking user must be in the `kvm` group: `sudo usermod -aG kvm "$USER"` then re-login. bot-bottle preflights this and reports exactly what's missing.
- **`firecracker`** on `PATH`: grab a release from <https://github.com/firecracker-microvm/firecracker/releases>. Start flows print this pointer when the binary is missing.
- **e2fsprogs** (`mke2fs`, `debugfs`, `e2fsck`, `resize2fs`) for building root filesystems. Docker is optional: it is used only to build the infra images from source rather than downloading a release's prebuilt ones.
- **A one-time privileged network setup** — the per-bottle TAP pool plus the fail-closed `nftables` isolation table. Run `bot-bottle install --backend=firecracker` for the host-appropriate config (a NixOS module, a `sudo` script elsewhere) plus autostart for the host controller; `bot-bottle backend status --backend=firecracker` reports what's present, including whether the pool range collides with an existing route. The pool defaults to `10.243.0.0/16` (an obscure RFC-1918 block that dodges docker/libvirt/LAN and, deliberately, Tailscale's `100.64.0.0/10` CGNAT range); override with `BOT_BOTTLE_FC_IP_BASE` if it clashes on your host.

```sh
BOT_BOTTLE_BACKEND=firecracker bot-bottle start <agent>
```

> **NixOS:** enable `virtualisation.docker`, ensure the KVM module is loaded (`boot.kernelModules = [ "kvm-intel" ];` or `kvm-amd`), and add your user to the `kvm` and `docker` groups. For the network pool, consume the flake module — `imports = [ inputs.bot-bottle.nixosModules.firecracker-netpool ]; services.bot-bottle-firecracker = { enable = true; owner = "you"; };` — then `nixos-rebuild switch` (imperative nft/TAP rules don't survive a rebuild; channel users can `imports = [ <bot-bottle>/nix/firecracker-netpool.nix ]`). `firecracker` isn't in nixpkgs by default as a user binary — install the release binary (pin the version) and put it on `PATH`.

> **CI:** Firecracker integration runs in the manually dispatched `.gitea/workflows/pre-release-test.yml` on a self-hosted runner labelled `kvm`; privileged KVM hosts never execute unreviewed PR code automatically. Provision it like a normal Firecracker host: `firecracker` on `PATH`, `/dev/kvm`, the cached guest kernel and static dropbear, and the persistent TAP/nft pool. The required pull-request workflow runs unit plus the complete Docker integration suite on `ubuntu-latest`; see `docs/ci.md`.

```sh
bot-bottle start <agent>   # builds the image on first run, drops you into claude
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
      inspect:
        auth:
          scheme: token        # Bearer | token
          token_ref: BOT_BOTTLE_GITEA_TOKEN
        matches:               # optional — restrict to specific paths/methods/headers
          - paths:
              - {type: prefix, value: /api/v1/}
            methods: [GET, POST, PATCH, DELETE]
        outbound_detectors: [token_patterns, known_secrets]
        inbound_detectors: false   # disable response scanning for this host
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

**Egress route fields:**

| Field | Required | Description |
|---|---|---|
| `host` | yes | Hostname to allowlist. One entry per host. |
| `role` | no | Reserved for future use. The key is recognised but any value is currently rejected at load. Provider auth routes (e.g. Claude's `api.anthropic.com`) are injected automatically from `agent_provider.auth_token`, not via `role`. |
| `auth.scheme` | when `auth` present | `Bearer` or `token`. Injected by the proxy; the agent never sees the value. |
| `auth.token_ref` | when `auth` present | Env-var name holding the secret on the host. |
| `matches` | no | Array of `{paths, methods, headers}` filters. A request must match at least one entry (if any are given) to be forwarded. |
| `matches[].paths` | no | Array of `{type, value}`. `type` is `prefix` (default), `exact`, or `regex`. |
| `matches[].methods` | no | Array of HTTP method strings, e.g. `[GET, POST]`. |
| `matches[].headers` | no | Array of `{name, value, type}`. `type` is `exact` (default) or `regex`. |
| `dlp` | no | Per-route DLP overrides. Omit to use defaults (all detectors on). |
| `dlp.outbound_detectors` | no | `false` disables outbound scanning; list restricts to named detectors (`token_patterns`, `known_secrets`). |
| `dlp.inbound_detectors` | no | `false` disables inbound scanning; list restricts to named detectors (`naive_injection_detection`). |
| `dlp.outbound_on_match` | no | What to do when an outbound token is detected: `supervise` (default for manifest routes — hold for operator approval), `redact` (scrub the value and forward), or `block` (hard 403). Agent-provider routes (e.g. `api.anthropic.com`) default to `redact`. |
| `git.fetch` | no | `true` permits smart HTTP clone/fetch (`git-upload-pack`) for this host. Push (`git-receive-pack`) remains blocked. |

When an outbound DLP detector matches a token, the route's `dlp.outbound_on_match` policy decides what happens. Under the default `supervise`, the proxy queues an `egress-token-allow` proposal for the operator's `bot-bottle supervise` TUI and holds the request open until it is answered (or `EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS`, default 300s, elapses — after which it fails closed). The operator never sees the raw token, only the host, method, path, and a redacted snippet; approving adds the value to an in-memory safelist for the life of the egress proxy. Under `redact`, the matched value is scrubbed from the body, headers, and path and the request is forwarded (failing closed if a match lands somewhere unredactable, like the hostname). Under `block` it stays a hard `403`. Structural blocks (CRLF injection) and not-in-allowlist host blocks are always hard `403`s regardless of policy.

More examples in `examples/`. Full design lives under `docs/prds/`; the trust-boundary rationale is in `docs/prds/0011-per-file-md-manifest.md`.

## Tracker policy

Issues are the canonical work items and own all tracker labels; every issue
must have at least one. Pull requests stay unlabeled and deliberately reference
an issue with `Closes #…`, `Part of #…`, or another form defined in
[`ADR 0005`](docs/decisions/0005-issues-own-tracker-metadata.md). Gitea Actions
enforces the convention for new work from 2026-07-18 onward. Earlier closed
PRs are grandfathered rather than given artificial retrospective issues.

## Trademarks

bot-bottle is an independent project and is not affiliated with, endorsed by, or sponsored by Anthropic, PBC. "Claude" and "Claude Code" are trademarks of Anthropic, PBC; the project name uses "claude" descriptively to indicate that the tool runs Claude Code inside a sandbox.

## License

Copyright 2026 Eric Bauerfeld. Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text.
