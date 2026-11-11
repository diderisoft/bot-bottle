# Glossary

Prefer these names in docs, comments and UI.

## Agent / Agent Definition

A Markdown file with YAML frontmatter that declares the model harness's
system prompt, identity, skills and Agent Provider. It lives under
`~/.bot-bottle/agents/` or a repo's `.bot-bottle/agents/`.

It names no bottle and holds no secrets. You pick bottles at launch
([manifest.md](manifest.md#agents)). A project agent whose provider grants
something needs per-host trust, covering the files its image build takes
from the repo ([projects.md](projects.md)).

## Agent Provider

The component that connects to a model provider and sets up the harness in
the agent runtime: auth, startup args and egress routes. It is set in an
agent's `agent_provider:` (`harness: claude`, or the shorthand
`agent_provider: claude`).

Built-in harnesses are `claude` (the default), `codex` and `pi`. A bottle
can't declare one, so one bottle serves any harness.

## Agent Runtime

The OCI image, built from the agent provider's Dockerfile, and the container
or VM that runs it.

| Backend | Agent runtime |
|---|---|
| macOS-container | an Apple Container |
| Firecracker | a microVM |
| legacy Docker | a Docker container |

## Architect (Bottle Architect)

The agent behind `bot-bottle bottles suggest` and `bot-bottle start`. Given a
task, it proposes the bottle the task needs, which you review before it runs.
`start --auto` skips the review. It runs in its own tiny bottle with only its
model API ([architect.md](architect.md)).

## Bottle / Bottle Definition

A Markdown file with YAML frontmatter that declares one agent runtime's
boundaries: egress allowlist, git remotes, env vars and nested-container
flag. It lives under `~/.bot-bottle/bottles/` or a repo's
`.bot-bottle/bottles/`.

A project bottle is named `./<name>`, so it never shadows one of yours. It
runs only once this host trusts its exact contents
([projects.md](projects.md)).

## Bottled Agent

An Agent Definition and a Sealed Bottle under one identity: the thing that
runs, stops and resumes. A bottle is never the running thing.

`bot-bottle bottled-agents`, or `bots`, lists them. To **bottle** an agent is
to start one: "Bottle agent" in the console, `bot-bottle start` in the CLI
([ADR 0009](../decisions/0009-bottled-agent-terminology.md)).

| State | Means |
|---|---|
| **Running** | its agent runtime is up (`bots --running`) |
| **Stopped** | its runtime is gone, but its state and [home](backends.md#the-agents-home) are kept, so `bot-bottle resume` continues its last conversation (`bots --stopped`) |
| **Left over** | stopped, and its session didn't end cleanly. Resumable, but `cleanup bots` removes it. |
| **Frozen** | its runtime was snapshotted (`bot-bottle commit`), so a resume boots from the snapshot |

## Canary

A per-session decoy secret in the bottle's env. Any outbound appearance of it
is treated as exfiltration and always blocked ([gateway.md](gateway.md)). The
launch's isolation probe also uses a canary listener the agent must not reach
([backends.md](backends.md)).

## Console

The web UI, in a browser or on a phone, that a host pairs with
(`bot-bottle pair`). From it you launch and watch bottled agents and answer
approval cards. The host controller dials out to it over iroh and accepts no inbound
connections ([host-controller.md](host-controller.md#remote-access)).

## Environment

One independent install on a host, picked by `BOT_BOTTLE_ENV`: `live` (the
default) or a named one with its own state root, names and network
([install.md](install.md#environments)).

## Forge

A git host (Gitea or GitHub) connected with `bot-bottle forge connect`: a
checked token, a pinned SSH host key and the user it pushes as. Bottles grant
repos on it, and the host supplies the credentials
([manifest.md](manifest.md#forges)).

## Gateway

The per-host service every agent's traffic goes through: the egress proxy
(allowlist, DLP, token injection), git-gate and the supervise MCP server. It holds the real credentials so the
agent never does ([gateway.md](gateway.md)).

## Grant

A one-line repo entry in `git_gate.repos`, such as
`git.example.com/acme/app:write`: forge host, repo and whether the agent may
push. It names no user or token
([git-gate.md](git-gate.md#forges-and-one-line-grants)).

## Host Controller

The per-host daemon, running as the user, behind every bottle operation from
the CLI, the orchestrator and the console. It owns launch, teardown,
sessions, attach and the audit journal. It adds no privilege and is not a
security boundary ([host-controller.md](host-controller.md)).

## Orchestrator

The per-host control-plane service. It holds the bottle registry, encrypted
egress tokens, the supervise queue and the signing key. It is the only
opener of its database, `registry.db`. With
the gateway it forms the **infra pair**
([architecture.md](architecture.md#components)).

## Sealed Bottle

The fully resolved bottle, after `extends:` inheritance and validation. It is
the immutable boundary spec the launcher enforces.

## Session

One conversation of the harness inside a bottled agent, with its own
transcript. A bottled agent can hold many. A session is neither the bottled
agent nor its terminal.

## Supervise / Approval Card

How a running agent asks for more than its bottle grants: a new egress host,
repo or forge, through MCP tools on the gateway. Each request becomes an
approval card in `bot-bottle supervise`, the terminal's popup or the console.
Nothing changes until you approve ([gateway.md](gateway.md#supervise)).

## Terminal

The host controller's terminal for a running bottled agent, which
`bot-bottle attach` joins and the console relays. It is `open` while the
harness runs and `exited` after.
