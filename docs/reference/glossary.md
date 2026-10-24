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
a secret, an image build) needs per-host trust, like a repo bottle.

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

The combination of an Agent Definition and a Sealed Bottle, representing a
single deployable agent with a fixed identity and fixed boundaries. A bottled
agent has two observable states:

- **Active** — the agent runtime is running and the agent is executing.
- **Frozen** — the agent runtime has been snapshotted (Firecracker committed
  image); the agent is not running but can be resumed from the snapshot.

## Active Bottle

Shorthand for an active (running) Bottled Agent. Used in the supervisor TUI
and discovery layer to mean "a bottle whose agent runtime is currently up."
