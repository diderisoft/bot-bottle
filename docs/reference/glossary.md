# Glossary

Canonical terminology for bot-bottle. Prefer these names in docs, comments, and UI.

---

## Agent Provider

The component that connects to an external model provider and sets up the model
harness inside the agent runtime. Configured in an agent manifest under
`agent_provider:` (`harness: claude`, or the shorthand `agent_provider:
claude`); built-in harnesses are `claude`, `codex` and `pi`, and `claude` is
the default. Responsible for provider-specific auth, startup args, and egress
routes. A bottle can't declare it, so one bottle serves any harness.

## Agent Runtime

The OCI image (and the container or VM it runs in) that houses the model
harness. On the macOS-container backend this is an Apple Container; on
Firecracker it is a microVM; on the legacy Docker backend it is a Docker
container. The agent runtime is built from the agent provider's Dockerfile
(built-in or custom).

## Agent / Agent Definition

A Markdown file with YAML frontmatter that declares the system prompt and
identity of the model harness. Lives under `~/.bot-bottle/agents/` or a repo's
`.bot-bottle/agents/`. Specifies which bottle to run under (`bottle:`), which
skills to load, and its Agent Provider. Agent definitions contain no secrets;
a repo agent whose provider grants something (a host login, a model host with
a secret, an image build) needs per-host trust, like a repo bottle. The trust
covers the files its image build takes from the repo, not only the agent file.

## Bottle / Bottle Definition

A Markdown file with YAML frontmatter that declares the security and runtime
boundaries for one agent runtime: egress allowlist, git remotes, env vars,
and nested-container flag. Lives under `~/.bot-bottle/bottles/` or a repo's
`.bot-bottle/bottles/`. A repo bottle is always named `./<name>`, so it never
shadows one of yours, and it runs only once this host trusts its exact
contents; any change asks again, so a cloned repo can't widen egress without
you seeing it first ([manifest.md](manifest.md#trust)).

## Sealed Bottle

The fully-resolved bottle after all `extends:` inheritance is applied and every
field has been validated. The sealed bottle is the immutable boundary spec that
the launcher enforces — no further overrides are possible once it is sealed.

## Bottled Agent

The combination of an Agent Definition and a Sealed Bottle under one identity:
the thing that runs, stops and resumes. "Bottled agent" is the name in docs,
the console and CLI messages; the CLI's shorthand is `bots`
(`bot-bottle bottled-agents`, or `bots`, lists them; ADR 0009). To **bottle**
an agent is to start one ("Bottle agent" in the console, `bot-bottle start`).
A bottle (the definition) is never the running thing. A bottled agent is:

- **Running:** its agent runtime is up (`bots --running`).
- **Stopped:** its runtime is gone but its state and its home (its
  conversation and workspace, see [backends](backends.md#the-agents-home))
  are kept, so `bot-bottle resume <bottled agent>` starts it again, continuing
  its last conversation (`bots --stopped`). A stopped one
  whose session didn't end cleanly is **left over**: resumable, but
  `cleanup bots` removes it.
- **Frozen:** its runtime has been snapshotted (`bot-bottle commit`), so a
  resume boots from the snapshot.

## Session

One conversation of the model harness inside a bottled agent: a Claude, Codex
or pi session, with its own transcript. A bottled agent can hold many. Not
the bottled agent itself, and not its terminal.

## Terminal

The host controller's terminal for a running bottled agent, the one
`bot-bottle attach` joins and the console relays: `open` while the harness
runs in it, `exited` after.

---

Components and other terms, alphabetical.

## Architect (Bottle Architect)

The agent behind `bot-bottle bottles suggest` and `bot-bottle start`: given
a task, it proposes the bottle the task needs as a typed proposal, which you
review before it runs (`start --auto` runs it without asking). It runs in its own tiny bottle with only its
model API ([bottles-suggest.md](bottles-suggest.md)).

## Canary

A per-session decoy secret in the bottle's env. Any outbound appearance of it
is treated as exfiltration and always blocked
([gateway.md](gateway.md)). Separately, the launch's isolation probe opens a
canary listener the agent must not reach ([backends.md](backends.md)).

## Console

The web UI (browser or phone) a host pairs with (`bot-bottle pair`). The host
controller dials out to it over iroh and accepts no inbound connections;
from it you launch and watch bottled agents and answer approval cards
([host-controller.md](host-controller.md#remote-access)).

## Environment

One independent install on a host, picked by `BOT_BOTTLE_ENV`: `live` (the
default) or a named one with its own state root, names and network, so
development or CI never touches the live install
([install.md](install.md#environments)).

## Forge

A git host (Gitea or GitHub) connected on this host with `bot-bottle forge
connect`: a checked token, a pinned SSH host key and the user it pushes as.
Bottles describe forges and grant repos on them; the host supplies the
credentials ([manifest.md](manifest.md#forges)).

## Gateway

The per-host service every agent's traffic goes through: the egress proxy
(allowlist, DLP, token injection), git-gate and the supervise MCP server. It
holds the real credentials so the agent never does
([gateway.md](gateway.md)).

## Grant

A one-line repo entry in a bottle's `git-gate.repos`, such as
`git.example.com/acme/app:write`: the forge host, the repo and whether the
agent may push. It names no user or token, so it stays portable
([git-gate.md](git-gate.md#forges-and-one-line-grants)).

## Host Controller

The per-host daemon, running as the user, behind every bottle operation: the
CLI, the orchestrator and the console all go through it. It owns launch,
teardown, sessions, attach and the audit journal. It adds no privilege and
is not a security boundary ([host-controller.md](host-controller.md)).

## Orchestrator

The per-host control-plane service: the bottle registry, encrypted egress
tokens, the supervise queue and the signing key; the only opener of
`bot-bottle.db`. With the gateway it forms the **infra pair**
([architecture.md](architecture.md#components)).

## Supervise / Approval Card

Supervise is how a running agent asks for more than its bottle grants (a new
egress host, a repo, a forge) through MCP tools on the gateway. Each request
is queued and shown to you as an approval card in `bot-bottle supervise`, the
terminal's popup or the console; nothing changes until you approve
([gateway.md](gateway.md#supervise)).
