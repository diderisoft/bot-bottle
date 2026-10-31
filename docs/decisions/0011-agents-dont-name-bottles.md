# ADR 0011: Agents don't name a bottle; a launch with none runs in `basic`

- **Status:** Accepted
- **Date:** 2026-10-31
- **Deciders:** didericis

## Context

An agent definition could name a bottle (`bottle: <name>`). A launch with no
bottles picked then ran in it, and the CLI, the console's launch page and the
host controller each fell back to it.

That coupled two things meant to be independent. An agent says who runs (the
harness, its login, skills and prompt). A bottle says what it may reach. With
`bottle:` set:
- **An agent file wasn't portable.** It only loaded on a host with a bottle of
  that name. Moving it to another host, sharing it, or syncing it through the
  console meant bringing or renaming the bottle too.
- **Policy hid in the agent.** Which egress, credentials and repos a launch got
  depended on a line in the agent file rather than on what was picked at
  launch.
- **Starters needed a bottle each.** `bot-bottle setup` wrote an empty bottle
  per starter agent, only so the agent had one to name.

## Decision

- Agent manifests have no `bottle` field. A file that still has one fails to
  load, saying to remove the line and pick the bottle at launch.
- Bottles are picked at launch: `--bottle` (repeatable), the `bot-bottle start`
  picker, or the console's launch page.
- A launch with no bottles picked runs in the built-in `basic` bottle
  (`DEFAULT_BOTTLE`). Bare `bot-bottle <agent>` runs in the repo's `./main`
  when it has one, else `basic`.
- `bot-bottle setup` writes starter agents only, no starter bottles.

## Consequences

- An agent file works on any host, and agent and bottle libraries can be
  shared and synced separately.
- A launch's policy is always visible at launch: the bottles picked, or
  `basic`.
- **Breaking:** existing agents with `bottle:` stop loading until the line is
  removed. A launch that relied on the agent's bottle now needs `--bottle`
  (or the picker), and bare `bot-bottle <agent>` no longer merges it in.
- Starter bottles already on disk are left alone. They're ordinary bottles,
  selectable by name.

## Links

- [manifest.md](../reference/manifest.md#agents)
- PRD 0115 (design repo), versioned manifests: the general mechanism for
  format changes like this one.
