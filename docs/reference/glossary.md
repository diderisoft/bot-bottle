# Glossary

Canonical terminology for bot-bottle. Prefer these names in docs, comments, and UI.

## Agent / Agent Definition

A Markdown file with YAML frontmatter that declares the system prompt and
identity of the model harness. It lives under `~/.bot-bottle/agents/` or a
repo's `.bot-bottle/agents/`.

It specifies which skills to load and its Agent Provider. It names no bottle:
you pick bottles at launch ([manifest.md](manifest.md#agents)).

Agent definitions contain no secrets. A repo agent whose provider grants
something (a host login, a model host with a secret, an image build) needs
per-host trust, like a repo bottle.

The trust covers the files its image build takes from the repo, not only the
agent file.

## Agent Provider

The component that connects to an external model provider and sets up the
model harness inside the agent runtime. It handles provider-specific auth,
startup args, and egress routes.

It is configured in an agent manifest under `agent_provider:`, as
`harness: claude` or the shorthand `agent_provider: claude`. Built-in harnesses
are `claude`, `codex` and `pi`, and `claude` is the default.

A bottle can't declare it, so one bottle serves any harness.

## Agent Runtime

The OCI image, and the container or VM it runs in, that houses the model
harness. It is built from the agent provider's Dockerfile (built-in or custom).

| Backend | Agent runtime |
|---|---|
| macOS-container | an Apple Container |
| Firecracker | a microVM |
| legacy Docker | a Docker container |

## Architect (Bottle Architect)

The agent behind `bot-bottle bottles suggest` and `bot-bottle start`. Given a
task, it proposes the bottle the task needs as a typed proposal, which you
review before it runs.

`start --auto` runs it without asking. It runs in its own tiny bottle with only
its model API ([bottles-suggest.md](bottles-suggest.md)).

## Bottle / Bottle Definition

A Markdown file with YAML frontmatter that declares the security and runtime
boundaries for one agent runtime. Those are its egress allowlist, git remotes,
env vars, and nested-container flag.

It lives under `~/.bot-bottle/bottles/` or a repo's `.bot-bottle/bottles/`. A
repo bottle is always named `./<name>`, so it never shadows one of yours.

A repo bottle runs only once this host trusts its exact contents, and any
change asks again. So a cloned repo can't widen egress without you seeing it
first ([manifest.md](manifest.md#trust)).

## Bottled Agent

The combination of an Agent Definition and a Sealed Bottle under one identity:
the thing that runs, stops and resumes. A bottle (the definition) is never the
running thing.

"Bottled agent" is the name in docs, the console and CLI messages. The CLI's
shorthand is `bots`: `bot-bottle bottled-agents`, or `bots`, lists them
(ADR 0009).

To **bottle** an agent is to start one: "Bottle agent" in the console,
`bot-bottle start` in the CLI.

| State | Means | Listed by |
|---|---|---|
| **Running** | its agent runtime is up | `bots --running` |
| **Stopped** | its runtime is gone but its state and its home are kept, so `bot-bottle resume <bottled agent>` starts it again, continuing its last conversation | `bots --stopped` |
| **Left over** | stopped, and its session didn't end cleanly: resumable, but `cleanup bots` removes it | |
| **Frozen** | its runtime has been snapshotted (`bot-bottle commit`), so a resume boots from the snapshot | |

A stopped agent's home is its conversation and workspace
([backends](backends.md#the-agents-home)).

## Canary

A per-session decoy secret in the bottle's env. Any outbound appearance of it
is treated as exfiltration and always blocked ([gateway.md](gateway.md)).

Separately, the launch's isolation probe opens a canary listener the agent
must not reach ([backends.md](backends.md)).

## Console

The web UI, in a browser or on a phone, that a host pairs with
(`bot-bottle pair`). From it you launch and watch bottled agents and answer
approval cards.

The host controller dials out to it over iroh and accepts no inbound
connections ([host-controller.md](host-controller.md#remote-access)).

## Environment

One independent install on a host, picked by `BOT_BOTTLE_ENV`. It is `live`
(the default) or a named one with its own state root, names and network.

So development or CI never touches the live install
([install.md](install.md#environments)).

## Forge

A git host (Gitea or GitHub) connected on this host with
`bot-bottle forge connect`. A connection is a checked token, a pinned SSH host
key and the user it pushes as.

Bottles describe forges and grant repos on them. The host supplies the
credentials ([manifest.md](manifest.md#forges)).

## Gateway

The per-host service every agent's traffic goes through. It runs the egress
proxy (allowlist, DLP, token injection), git-gate and the supervise MCP server.

It holds the real credentials so the agent never does
([gateway.md](gateway.md)).

## Grant

A one-line repo entry in a bottle's `git_gate.repos`, such as
`git.example.com/acme/app:write`. It gives the forge host, the repo and
whether the agent may push.

It names no user or token, so it stays portable
([git-gate.md](git-gate.md#forges-and-one-line-grants)).

## Host Controller

The per-host daemon, running as the user, behind every bottle operation. The
CLI, the orchestrator and the console all go through it.

It owns launch, teardown, sessions, attach and the audit journal. It adds no
privilege and is not a security boundary
([host-controller.md](host-controller.md)).

## Orchestrator

The per-host control-plane service. It holds the bottle registry, encrypted
egress tokens, the supervise queue and the signing key, and is the only opener
of its own database, `registry.db`.

With the gateway it forms the **infra pair**
([architecture.md](architecture.md#components)).

## Sealed Bottle

The fully-resolved bottle after all `extends:` inheritance is applied and every
field has been validated.

The sealed bottle is the immutable boundary spec that the launcher enforces.
No further overrides are possible once it is sealed.

## Session

One conversation of the model harness inside a bottled agent: a Claude, Codex
or pi session, with its own transcript. A bottled agent can hold many.

A session is not the bottled agent itself, and not its terminal.

## Supervise / Approval Card

Supervise is how a running agent asks for more than its bottle grants, through
MCP tools on the gateway. It can ask for a new egress host, a repo or a forge.

Each request is queued and shown to you as an approval card. Cards appear in
`bot-bottle supervise`, the terminal's popup or the console. Nothing changes
until you approve ([gateway.md](gateway.md#supervise)).

## Terminal

The host controller's terminal for a running bottled agent. It is the one
`bot-bottle attach` joins and the console relays.

It is `open` while the harness runs in it, and `exited` after.
