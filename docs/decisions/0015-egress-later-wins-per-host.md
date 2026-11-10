# ADR 0015: A later bottle's egress route for a host replaces the earlier one

Syntax in this record predates v0.1.9; see [manifest.md](../reference/manifest.md).

- **Status:** Accepted
- **Date:** 2026-11-03
- **Deciders:** didericis

## Context

Bottles compose: a child `extends` parents, a launch can stack several
`--bottle`s. `env` and `git-gate.repos` already merged later-wins, by name.
Egress routes didn't. A child's routes were appended to its parents', and
the same host in both was a load error ("duplicate host … each host must be
unique on the proxy"). So a child could add hosts but never narrow, widen or
remove one it inherited. The only way to change a built-in bottle's route
was to stop extending it and copy the rest.

That rule also broke "Allow and keep". Keeping an approved route for a host
the bottle inherited wrote a second route for that host into the bottle,
which then failed to load at the next launch. And a launch stack of bottles
didn't check for duplicates at all, so two stacked bottles that both routed
a host went through unchecked. The suggester's validation stacks bottles the
same way.

The console's Bottle tab is getting an "All rules" view that edits any rule
in the chain. A rule from a built-in bottle can only be changed by
overriding it in a writable bottle later in the chain, which this rule
didn't allow.

## Decision

- **Later wins per host** (case-insensitive), everywhere bottles combine: a
  child over its parents, a later parent over an earlier one, a later stacked
  bottle over an earlier one. The replacing route takes the earlier route's
  place; hosts new to the later bottle follow, in its order.
- **`{host: x, blocked: true}` takes an inherited host away.** Egress is
  default-deny, so a host no route names is blocked. `blocked` must be `true`
  and takes no other key. A blocked entry is never a route: the parser keeps
  it apart (`ManifestEgressConfig.Blocked`), so nothing that reads routes can
  mistake it for one. A block also stays with the resolved bottle and its
  document, so a bottle stacked after it still sees the block. A later route
  for the host unblocks it. Blocking a host that nothing routes is allowed and
  does nothing (no warning: a bottle may block a host on purpose, against a
  parent that might add it later).
- **Within one bottle a host is still named once**, routed or blocked. A
  duplicate there is a typo, not an override.

## Consequences

- A child can narrow, replace or block an inherited route without copying
  its parent. "Allow and keep" for an inherited host now writes a route that
  replaces the inherited one. That route is the full one the operator
  approved, so nothing the parent allowed is lost. Allowing a host the bottle
  blocks swaps the block for the route.
- Accidental duplicates across bottles become silent overrides, not errors:
  a child that happens to name a parent's host replaces that parent's route
  without saying so. The console's All rules view shows each route's source
  and marks a route that overrides another, so an override can be seen where
  bottles are edited. `bot-bottle` itself doesn't warn.
- A resolved bottle's fingerprint changes only for bottles that block a
  host, since its document now lists the block.

## Links

- [manifest.md, composing bottles](../reference/manifest.md#bottles) and [egress routes](../reference/manifest.md#egress-routes).
