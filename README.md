<p align="center">
  <img src="docs/logo.svg" alt="claude-bottle logo" width="140">
</p>

# claude-bottle

[![test](https://gitea.dideric.is/didericis/claude-bottle/actions/workflows/test.yml/badge.svg?branch=main)](https://gitea.dideric.is/didericis/claude-bottle/actions?workflow=test.yml)

Run multiple Claude Code agents on your own machine, each scoped to its own secrets, skills, and egress allowlist.

![pipelock and git-gate blocking exfil attempts against a live bottle](docs/demo.gif)

Four prompts to the agent inside a real bottle:
claude replies to `hello there` — proof api.anthropic.com routes
through pipelock's bumped TLS end-to-end;
asked to GET a non-allowlisted host, the agent's curl gets 403 back
from pipelock;
asked to POST a credential-shaped body to an allowlisted host, the
same 403 — pipelock's DLP body scanner caught it;
asked to commit and push an AKIA-shaped key, git-gate's gitleaks
pre-receive hook rejects the ref.
Run it yourself with `bash scripts/demo.sh`.

## Why "claude-bottle"?

Each container is a bottle; Claude is the genie inside. The genie's
powers are exactly what the manifest grants it — a specific set of
skills, a specific set of secrets, and a specific set of hosts it can
reach — nothing more. You uncork one bottle per agent
(`./cli.py start <agent>`), many bottles run in parallel, and each is
scoped to its task. When the session ends the bottle is destroyed and
the genie does not persist.

## Goals

- Scope each agent to the minimum credentials and network egress its task actually needs
- Run multiple agents in parallel, isolated from each other
- Keep code, credentials, and agent activity on infrastructure I control — no third-party agent runtime

## Security model

Each agent runs in its own bottle: its own container, its own internal
Docker network, and its own pipelock sidecar. Bottles don't share
state, don't talk to each other, and only get the env vars, skills,
SSH identities, and egress hosts the manifest grants them — nothing
more. Any one agent only has the access it needs to do its job.

The bottle limits both what an agent can see and where it can send
it. Each bottle gets only the secrets and SSH identities the manifest
grants it — a Gitea token but not a GitHub token, a deploy key but
not a personal SSH key — so even a compromised or misbehaving agent
only handles credentials it was already trusted with for its job.
Egress flows through pipelock, which constrains where those
credentials can travel: an agent with a Gitea token can reach
`gitea.dideric.is`, not arbitrary attacker-controlled hosts. The same
constraint blocks DNS-over-HTTPS as an exfil channel — a DoH resolver
like `cloudflare-dns.com` would have to be on the allowlist for the
agent to reach it at all. The container itself adds a layer between
the agent and the host, but the v1 design leans more on secret
minimization and egress allowlisting than on the container as a
hardened boundary. On Linux hosts where [gVisor](https://gvisor.dev/)
is registered with Docker, claude-bottle auto-detects it and launches
every bottle under `runsc` for a userspace syscall barrier — no
manifest configuration required. The broader v2 discussion lives in
`docs/research/stronger-isolation-alternatives.md`.

The egress proxy and OAuth-token handling below are the load-bearing
pieces of v1.

## Architecture

A bottle is the agent container plus up to three per-protocol egress
sidecars on a per-agent Docker `--internal` network. The agent has no
default route off-box; its only way out is through the pipelock
sidecar (for HTTP/HTTPS), the ssh-gate sidecar (for SSH), or the
git-gate sidecar (for git operations against declared upstreams).
Each sidecar also sits on an egress network that does have internet
access, so the agent's traffic always passes through a container
that enforces the manifest before it leaves the host.

```
                            host  ( ./cli.py )
                                  │
                          starts  │  stops
                                  ▼
   ┌─────────────────────────── bottle ──────────────────────────┐
   │                                                             │
   │   ┌──────────────────┐                                      │
   │   │ agent image      │   HTTPS_PROXY    ┌────────────────┐  │  HTTPS to
   │   │ (claude-code,    │ ───────────────► │ pipelock image │──┼──►  allowlisted
   │   │  built locally)  │                  │ (TLS bump, DLP,│  │     hosts
   │   │                  │                  │  allowlist)    │  │
   │   │ skills, env,     │                  └────────────────┘  │
   │   │ ~/.gitconfig     │                                      │
   │   │                  │   git ops        ┌────────────────┐  │  SSH (push/
   │   │                  │ ───────────────► │ git-gate image │──┼──►  fetch) to
   │   │                  │                  │ (gitleaks +    │  │     bottle.git
   │   │                  │                  │  git daemon)   │  │     upstreams
   │   └──────────────────┘                  └────────────────┘  │
   │                                                             │
   │   agent on internal network (no default route);             │
   │   sidecars also attached to an egress network.              │
   └─────────────────────────────────────────────────────────────┘
```

- **agent image** — built from the repo `Dockerfile` (`node:22-slim`
  base) on first run; runs `claude` with the manifest-granted skills,
  env vars, and `~/.gitconfig` (the latter for the git-gate's
  `insteadOf` rules when `bottle.git` is set).
- **pipelock image** — per-agent sidecar. Terminates the agent's
  outbound HTTP/HTTPS, enforces the resolved allowlist, runs DLP
  scanning. Design in `docs/prds/0001-per-agent-egress-proxy-via-pipelock.md`
  and `docs/prds/0006-pipelock-tls-interception.md`.
- **git-gate image** — per-agent sidecar built on `zricethezav/gitleaks`
  (alpine + gitleaks + git-daemon + openssh-client). Runs
  `git daemon` over `git://` as a bidirectional mirror of each
  declared upstream. A pre-receive hook gitleaks-scans incoming
  refs and forwards clean refs to the real upstream over SSH; an
  access-hook runs `git fetch origin --prune` against the upstream
  before every upload-pack so an agent fetch returns whatever the
  upstream has *now* (fail-closed if unreachable). The agent's
  `~/.gitconfig` rewrites the real URL to the gate via `insteadOf`,
  so push, fetch, clone, and pull all route through. The agent
  never sees the upstream credential. If the upstream's hostname
  isn't resolvable from the gate container (e.g. a Tailscale-only
  host whose public DNS points elsewhere), pin its IP via
  `ExtraHosts: { "<hostname>": "<ip>" }` on the `bottle.git` entry —
  the gate's `/etc/hosts` gets the override while the agent's
  `insteadOf` rewrite still keys off the original hostname. Brought
  up only when `bottle.git` has entries. Design in
  `docs/prds/0008-git-gate.md`.

When the agent exits, `cli.py` tears down every sidecar that was
brought up and the two networks; nothing about a bottle persists
between runs.

## Quickstart

Requires Docker on the host and a long-lived Claude Code OAuth token in
your shell env.

```sh
./cli.py start <agent>   # builds the image on first run, drops you into claude
```

The container is removed automatically when the session ends. If the script
is killed with SIGKILL the exit trap won't fire and the container may be
left running; remove it with `docker rm -f <container-name>`.

## Manifest

Agents and the bottles they run in are declared in `claude-bottle.json`
in your project root or `$HOME` (both files merge if present, with
project entries overriding home entries on key conflict).

```jsonc
{
  "bottles": {
    "gitea-dev": {
      "env": {
        "GITEA_TOKEN":     "?paste your Gitea API token",
        "GITHUB_TOKEN":    "${GH_PAT}",
        "GIT_AUTHOR_NAME": "didericis"
      },

      "git": [
        {
          "Name":         "claude-bottle",
          "Upstream":     "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
          "IdentityFile": "/Users/didericis/.ssh/id_ed25519_gitea",
          "KnownHostKey": "ssh-ed25519 AAAA..."
        }
      ],

      // Egress is forced through a per-agent
      // [pipelock](https://github.com/luckyPipewrench/pipelock) sidecar
      // on a Docker `--internal` network — without the proxy the agent
      // has no route off-box. The effective allowlist is the union of
      // baked-in defaults (api.anthropic.com, claude.ai, ...) and the
      // hostnames listed here. Pipelock also runs DLP scanning and
      // detects URL-embedded high-entropy secrets. The resolved
      // allowlist is shown in the y/N preflight before launch.
      "egress": {
        "allowlist": [
          "github.com",
          "registry.npmjs.org",
          "pypi.org"
        ]
      }
    }
  },

  "agents": {
    "gitea-helper": {
      "bottle": "gitea-dev",
      "skills": ["init-prd"],
      "prompt": "You help maintain Gitea-hosted projects."
    }
  }
}
```

Comments are illustrative; the file itself must be valid JSON. See
`claude-bottle.example.json` for a working starting point. Pipelock's
design lives in `docs/prds/0001-per-agent-egress-proxy-via-pipelock.md`
and the rationale in `docs/research/pipelock-assessment.md`.

## Auth: OAuth token, not API key

claude-bottle authenticates `claude` inside the container with the same
Pro/Max subscription you already use on the host, via a long-lived OAuth
token. No `ANTHROPIC_API_KEY` is needed.

**Why a token instead of mounting `~/.claude.json`:** on macOS, Claude
Code stores OAuth credentials in the encrypted Keychain, not in
`~/.claude.json`. Mounting that file into a Linux container does not
carry the credentials with it. Linux hosts keep credentials in
`~/.claude/.credentials.json`, but to keep the launcher portable
claude-bottle uses the env-var path on every host.

**One-time setup on the host:**

```sh
claude setup-token   # browser login, prints a ~1-year OAuth token
```

Stash the token in your shell env (e.g. `~/.zshrc` or a secret manager)
as `CLAUDE_BOTTLE_OAUTH_TOKEN`:

```sh
export CLAUDE_BOTTLE_OAUTH_TOKEN="<token>"
```

`cli.py` automatically forwards it to every container as
`CLAUDE_CODE_OAUTH_TOKEN` via `docker run -e` — no manifest wiring
required, and the value is never written to disk or placed on argv.

Inside the container, `claude` picks up `CLAUDE_CODE_OAUTH_TOKEN` and
authenticates against your subscription. Caveats: the token is bound
to your subscription tier (Pro/Max/Team/Enterprise), it does not work
with `claude --bare` (which only reads `ANTHROPIC_API_KEY`), and if it
leaks, regenerate via `claude setup-token` again. Reference:
<https://code.claude.com/docs/en/authentication>.

## Trademarks

claude-bottle is an independent project and is not affiliated with,
endorsed by, or sponsored by Anthropic, PBC. "Claude" and "Claude
Code" are trademarks of Anthropic, PBC; the project name uses
"claude" descriptively to indicate that the tool runs Claude Code
inside a sandbox.

## License

Copyright 2026 Eric Bauerfeld

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE)
for the full text.
