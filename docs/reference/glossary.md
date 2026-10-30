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
and nested-container flag. Lives under
`~/.bot-bottle/bottles/`. Bottles are scoped to `$HOME` so a cloned repo
cannot override host egress policy.

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
