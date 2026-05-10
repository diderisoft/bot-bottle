<p align="center">
  <img src="docs/logo.svg" alt="claude-bottle logo" width="140">
</p>

# claude-bottle

[![test](https://gitea.dideric.is/didericis/claude-bottle/actions/workflows/test.yml/badge.svg?branch=main)](https://gitea.dideric.is/didericis/claude-bottle/actions?workflow=test.yml)

Spins up an isolated container for running Claude Code with a curated set of skills and env vars.

## Why "claude-bottle"?

Each container is a bottle; Claude is the genie inside. The genie has
broad powers within the bottle — read, write, run anything — but it
cannot escape to the host. You uncork one bottle per agent
(`./cli.py start <agent>`), many bottles run in parallel, and each
one's powers are scoped to what the manifest grants it: a curated set
of skills, env vars, and a starting prompt. When the session ends the
bottle is destroyed and the genie does not persist.

## Goals

- Minimize risk of running claude with full permissions
- Allow me to easily spin up agent tasks in parallel
- Create isolated, well defined, easily updated, shareable agents

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
`gitea.dideric.is`, not arbitrary attacker-controlled hosts. The
container itself adds a layer between the agent and the host, but the
v1 design leans more on secret minimization and egress allowlisting
than on the container as a hardened boundary. Linux hosts can opt into
[gVisor](https://gvisor.dev/) per bottle (see `runtime` in the
manifest below) for a userspace syscall barrier; the broader v2
discussion lives in `docs/research/stronger-isolation-alternatives.md`.

The egress proxy and OAuth-token handling below are the load-bearing
pieces of v1.

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
      // Container runtime for the agent. Default "runc"; set to
      // "runsc" on Linux hosts to launch the agent under gVisor for
      // a userspace syscall barrier between the agent and the host
      // kernel. claude-bottle verifies the runtime is registered with
      // Docker before launch; gVisor is not available on macOS.
      "runtime": "runsc",

      "env": {
        "GITEA_TOKEN":     "?paste your Gitea API token",
        "GITHUB_TOKEN":    "${GH_PAT}",
        "GIT_AUTHOR_NAME": "didericis"
      },

      "ssh": [
        {
          "Host":         "gitea",
          "Hostname":     "gitea.dideric.is",
          "User":         "git",
          "Port":         30009,
          "IdentityFile": "/Users/didericis/.ssh/id_ed25519_gitea",
          "KnownHostKey": "gitea.dideric.is ssh-ed25519 AAAA..."
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
