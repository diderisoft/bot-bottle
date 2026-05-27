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

A bottle is two containers per agent: an `agent` container, and a
`sidecars` container that bundles pipelock + egress + git-gate +
supervise behind a Python init supervisor (PRD 0024). They share a
per-agent Docker `--internal` network; the agent has no default
route off-box. All HTTP and HTTPS egress funnels through pipelock,
where the egress allowlist, TLS interception, and request-body DLP
scanner enforce the manifest before any byte leaves the host. The
only egress that doesn't traverse pipelock is git-gate's SSH
push/fetch to `bottle.git` upstreams — pipelock can't proxy SSH,
so git-gate is its own L4-style egress path with gitleaks doing
the pre-receive scan.

The agent dials the bundle by the legacy short names (`pipelock`,
`egress`, `git-gate`, `supervise`); the renderer registers those as
docker-network aliases on the bundle so existing HTTPS_PROXY URLs
and MCP endpoints resolve without an agent-side change.

```
                            host  ( ./cli.py )
                                  │
                          starts  │  stops
                                  ▼
   ┌─────────────────────────── bottle ──────────────────────────────────┐
   │                                                                     │
   │   ┌──────────────────┐                                              │
   │   │ agent image      │  HTTPS_PROXY                                 │
   │   │ (claude-code,    │ ────────────────────────┐                    │
   │   │  built locally)  │                         │                    │
   │   │                  │   plain HTTP            │                    │
   │   │ skills, env,     │  (token injection) ┌────▼─────────┐          │
   │   │ ~/.gitconfig,    │ ──────────────────►│ cred-proxy   │          │
   │   │ ~/.npmrc, tea    │                    │ (strips/inj  │          │
   │   │                  │                    │  Authoriz.)  │          │
   │   │ environ: URLs    │                    └─────┬────────┘          │
   │   │ only, no real    │     HTTPS_PROXY          │                   │
   │   │ tokens           │                          ▼                   │
   │   │                  │                  ┌────────────────┐          │  HTTPS to
   │   │                  │                  │ pipelock image │──────────┼──►  allowlisted
   │   │                  │                  │ (TLS bump, DLP │          │     hosts (incl.
   │   │                  │                  │  body scan,    │          │      cred-proxy
   │   │                  │                  │  allowlist)    │          │      upstreams)
   │   │                  │                  └────────────────┘          │
   │   │                  │                                              │
   │   │                  │   git://         ┌────────────────┐          │  SSH push/fetch
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
- **cred-proxy image** — per-bottle sidecar (`python:3.13-alpine`
  base, stdlib-only) that holds API tokens declared in
  `bottle.cred_proxy.routes`. Each route names a `path`,
  `upstream`, `auth_scheme`, and `token_ref` (host env var); the
  agent dials `http://cred-proxy:9099<path>...` over plain HTTP
  and the proxy strips any inbound `Authorization`, injects
  `<auth_scheme> <token>` using the value held only in its own
  container's environ, and forwards to the real upstream over
  HTTPS. SSE responses stream back unbuffered. The cred-proxy's
  outbound HTTPS routes through pipelock (it trusts pipelock's
  per-bottle CA), so pipelock's egress allowlist + body scanner
  apply to cred-proxy traffic the same way they apply to direct
  agent traffic. Smart-HTTP push paths (`/git-receive-pack`,
  `/info/refs?service=git-receive-pack`) are refused at the
  proxy — push must go through `bottle.git` / git-gate where
  gitleaks runs. Optional per-route `role` tags drive agent-side
  rewrites: `anthropic-base-url`, `npm-registry`, `git-insteadof`,
  `tea-login`. The agent's `printenv` shows only proxy URLs —
  none of the real token values. Design in
  `docs/prds/0010-cred-proxy.md`.

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

### Smolmachines backend (experimental, macOS-only)

A second backend runs the agent in a smolvm micro-VM (libkrun) with the
sidecar bundle still in Docker. Selected via
`CLAUDE_BOTTLE_BACKEND=smolmachines ./cli.py start <agent>`. Requires
`smolvm` on PATH (`curl -sSL https://smolmachines.com/install.sh | sh`).

The integration tests run against whichever backend the env var
selects and skip cleanly when its prerequisites are missing.

**One-time sudo on first launch (macOS):** smolmachines needs
per-bottle loopback aliases (`127.0.0.16` .. `127.0.0.31`) on `lo0`
so each bottle's TSI allowlist scopes to its own /32. The first
`./cli.py start` after each reboot prompts for sudo to add the pool
via `ifconfig lo0 alias`. Aliases persist until reboot; subsequent
launches don't prompt. Without this, every bottle would share
`127.0.0.1` and be able to reach unrelated host services on the
loopback.

## Manifest

Bottles and agents live as Markdown files with YAML frontmatter under
`~/.claude-bottle/`. Each bottle is one file in `bottles/`, each agent
is one file in `agents/`:

```
~/.claude-bottle/
├── bottles/
│   ├── dev.md
│   └── gitea-dev.md
└── agents/
    ├── implementer.md
    └── researcher.md
```

The filename (without `.md`) is the entity's name. Filenames must
match `[a-z][a-z0-9-]*`; files that don't are skipped with a warning.

A repo can ship its own agent files alongside its code at
`<repo>/.claude-bottle/agents/<name>.md`. Those agents reference
bottles defined in `~/.claude-bottle/bottles/` (the only place
bottles can come from); a `bottles/` subdir in a repo is ignored
with a warning. **This is the trust boundary**: bottle infrastructure
— credentials, egress allowlists, git remotes — comes from your home
directory only. A cloned repo cannot redirect a host env var to an
attacker-named upstream because it has no way to declare a bottle.

### Example bottle (`~/.claude-bottle/bottles/gitea-dev.md`)

````markdown
---
env:
  GIT_AUTHOR_NAME: didericis

git:
  - Name: claude-bottle
    Upstream: ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git
    IdentityFile: /Users/didericis/.ssh/id_ed25519_gitea
    KnownHostKey: ssh-ed25519 AAAA...

# Routes declared here are held by a per-bottle cred-proxy sidecar,
# not the agent. Each route names a path the agent dials, the
# upstream the proxy forwards to, an auth_scheme, and a token_ref
# (host env var). The value goes into the sidecar's environ via
# `docker create -e`, never touches argv or disk. Optional `role`
# tags drive agent-side rewrites: anthropic-base-url (sets
# ANTHROPIC_BASE_URL), npm-registry (writes ~/.npmrc), git-insteadof
# (writes ~/.gitconfig), tea-login (writes ~/.config/tea/config.yml).
# See docs/prds/0010-cred-proxy.md.
cred_proxy:
  routes:
    - path: /anthropic/
      upstream: https://api.anthropic.com
      auth_scheme: Bearer
      token_ref: CLAUDE_BOTTLE_OAUTH_TOKEN
      role: anthropic-base-url
    - path: /gh-api/
      upstream: https://api.github.com
      auth_scheme: Bearer
      token_ref: GH_PAT
    - path: /gh-git/
      upstream: https://github.com
      auth_scheme: Bearer
      token_ref: GH_PAT
      role: git-insteadof
    - path: /npm/
      upstream: https://registry.npmjs.org
      auth_scheme: Bearer
      token_ref: NPM_TOKEN
      role: npm-registry

# Egress is forced through a per-agent pipelock sidecar on a Docker
# `--internal` network — without the proxy the agent has no route
# off-box. The effective allowlist is the union of baked-in defaults
# (api.anthropic.com, claude.ai, ...) and the hostnames listed here.
# Pipelock also runs DLP scanning and detects URL-embedded
# high-entropy secrets. The resolved allowlist is shown in the y/N
# preflight before launch.
egress:
  allowlist:
    - github.com
    - registry.npmjs.org
    - pypi.org
---

The `gitea-dev` bottle. Backs my work on personal projects: Anthropic
OAuth via cred-proxy, gitea.dideric.is over SSH (with PAT for tea
API), and npm for publishing scoped packages.
````

### Example agent (`~/.claude-bottle/agents/gitea-helper.md`)

````markdown
---
bottle: gitea-dev
skills:
  - init-prd
---

You help maintain Gitea-hosted projects.
````

The agent's Markdown body is its system prompt (whitespace
stripped). The frontmatter declares the bottle to launch in and any
skills to mount. You can also include Claude Code subagent fields
(`name`, `description`, `model`, `color`, `memory`) in the
frontmatter — claude-bottle ignores them at launch but doesn't
reject them, so the same file can drop into `~/.claude/agents/` as a
Claude Code subagent.

Unknown top-level frontmatter keys die at load with a "did you mean"
pointer; typos don't silently ghost into an empty config.

The YAML subset the frontmatter accepts is bounded (flat keys,
strings / ints / true-or-false bools / null / lists / one-level
nested dicts). Anchors, multi-line block scalars, tags, and
ambiguous bare strings (`yes` / `NO` / `2026-05-24` /
`0x...`) all die with a clear pointer at the spec — quote your
strings when in doubt. The full schema lives in
`claude_bottle/yaml_subset.py` (~450 lines, stdlib-only, no PyYAML).

Working examples live under `examples/`. Pipelock's design lives in
`docs/prds/0001-per-agent-egress-proxy-via-pipelock.md` and the
rationale in `docs/research/pipelock-assessment.md`. The trust
boundary rationale lives in `docs/prds/0011-per-file-md-manifest.md`.

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

The bottle reaches the Anthropic API only through the cred-proxy
sidecar. To let `claude` authenticate, declare a route in
`bottle.cred_proxy.routes` with `role: "anthropic-base-url"` and
`token_ref: "CLAUDE_BOTTLE_OAUTH_TOKEN"`:

```jsonc
{
  "path":        "/anthropic/",
  "upstream":    "https://api.anthropic.com",
  "auth_scheme": "Bearer",
  "token_ref":   "CLAUDE_BOTTLE_OAUTH_TOKEN",
  "role":        "anthropic-base-url"
}
```

At launch, `cli.py` reads `CLAUDE_BOTTLE_OAUTH_TOKEN` from the host
env and forwards it into the cred-proxy container's environ — never
into the agent's. The agent receives `ANTHROPIC_BASE_URL` pointing at
`http://cred-proxy:9099/anthropic` and a non-secret placeholder for
`CLAUDE_CODE_OAUTH_TOKEN` (claude-code refuses to start without one;
the proxy strips and replaces the header on every request). `printenv`
inside the agent does not surface the real token, and the value is
never written to disk or placed on argv on the host.

A bottle without an `anthropic-base-url` route has no path to the
Anthropic API — there is no fallback that forwards the token directly
to the agent. Caveats: the token is bound to your subscription tier
(Pro/Max/Team/Enterprise), it does not work with `claude --bare`
(which only reads `ANTHROPIC_API_KEY`), and if it leaks, regenerate
via `claude setup-token` again. Reference:
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
