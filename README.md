# claude-bottle

[![test](https://gitea.dideric.is/didericis/claude-bottle/actions/workflows/test.yml/badge.svg?branch=main)](https://gitea.dideric.is/didericis/claude-bottle/actions?workflow=test.yml)

Spins up an isolated container for running Claude Code with a curated set of skills and env vars.

## Why "claude-bottle"?

Each container is a bottle; Claude is the genie inside. The genie has
broad powers within the bottle — read, write, run anything — but it
cannot escape to the host. You uncork one bottle per agent
(`./cli.sh start <agent>`), many bottles run in parallel, and each
one's powers are scoped to what the manifest grants it: a curated set
of skills, env vars, and a starting prompt. When the session ends the
bottle is destroyed and the genie does not persist.

## Goals

- Minimize risk of running claude with full permissions
- Allow me to easily spin up agent tasks in parallel
- Create isolated, well defined, easily updated, shareable agents

## Non-goals

- Communicating between agents directly
- Self hosted VMs (v1 uses local Docker containers, not VMs)
- Advanced agent auditing (lean on git history for auditing)

## Quickstart

Requires Docker on the host and a long-lived Claude Code OAuth token in
your shell env.

```sh
./cli.sh start <agent>   # builds the image on first run, drops you into claude
```

The container is removed automatically when the session ends. If the script
is killed with SIGKILL the exit trap won't fire and the container may be
left running; remove it with `docker rm -f <container-name>`.

## Egress

Agent containers route HTTP / HTTPS traffic through a per-agent
[pipelock](https://github.com/luckyPipewrench/pipelock) sidecar
attached to a Docker `--internal` network. The sidecar enforces a
hostname allowlist, runs DLP scanning (48 default credential
patterns), and detects URL-embedded high-entropy secret leaks. Without
the proxy the agent has no route off-box at all — the internal network
has no default gateway. The sidecar and network are torn down with the
agent on session exit.

The effective allowlist is the union of a baked-in default for Claude
Code's required hosts (`api.anthropic.com`, `claude.ai`, ...) and the
optional `bottles.<name>.egress.allowlist` field in
`claude-bottle.json`:

```jsonc
{
  "bottles": {
    "default": {
      "env":    { },
      "ssh":    [ ],
      "egress": { "allowlist": ["github.com"] }
    }
  }
}
```

The resolved allowlist is shown in the y/N preflight before launch.
See `docs/prds/0001-per-agent-egress-proxy-via-pipelock.md` for the
design and `docs/research/pipelock-assessment.md` for the rationale.

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

`cli.sh` automatically forwards it to every container as
`CLAUDE_CODE_OAUTH_TOKEN` via `docker run -e` — no manifest wiring
required, and the value is never written to disk or placed on argv.

Inside the container, `claude` picks up `CLAUDE_CODE_OAUTH_TOKEN` and
authenticates against your subscription. Caveats: the token is bound
to your subscription tier (Pro/Max/Team/Enterprise), it does not work
with `claude --bare` (which only reads `ANTHROPIC_API_KEY`), and if it
leaks, regenerate via `claude setup-token` again. Reference:
<https://code.claude.com/docs/en/authentication>.
