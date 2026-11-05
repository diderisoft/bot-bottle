# ADR 0017: Suggested bottles aren't saved unless you ask

- **Status:** Proposed
- **Date:** 2026-11-05
- **Deciders:** didericis

## Context

The guided `bot-bottle start` asks the bottle architect for a bottle for
each task. Most tasks are one-offs. If every accepted suggestion became a
bottle in the host's library, the library would fill with near-duplicates
nobody chose to keep, and every listing, picker and later suggestion would
have to wade through them. The launcher only took named bottles, so running
a suggestion without a library file wasn't possible.

## Decision

A suggestion runs as the run's own bottle and is saved to the library only
when you ask ("Save it as a bottle, then run it", or `bottles suggest
--write NAME`).

- The launch request carries the bottle's text (`LaunchRequest.run_bottle`,
  `cli` role only, at most 64 KiB), named `suggested` in its bottle list.
- The host controller stages it where only that launch's manifest index
  looks (`ManifestIndex.extra_bottle_dirs`), then keeps it in the bottle's
  state dir (`state/<id>/bottles/suggested.md`), so a resume loads it again
  and the console can show it.
- It never enters the library: no `bottles` listing, picker or suggestion
  sees it. The console lists it as kind `run`, read-only, and "keep" never
  writes into it.
- Its full text is in the run's audit (`lifecycle.bottled_agent_start`,
  `bottle_versions.run_bottle`), and the run's resolved manifest records
  what it resolved to.
- It's the user's own bottle, written by their CLI from what their host
  proposed, so it loads as a host bottle does. No project trust applies, and
  no other role can send one.

## Consequences

- The library holds only what someone chose to keep.
- A suggestion that turns out to be worth keeping has to be saved
  deliberately. A later idea is to offer saving the union of similar
  bottles when recent tasks keep producing similar boundaries (PRD 0117,
  future work).
- Approvals that "keep" a route can't keep it into an unsaved bottle; they
  go to the run's next host bottle, or aren't offered. The run's own copy
  is fixed for its lifetime.
- A host bottle named `suggested` is shadowed for a run that uses the name.

## Links

- [bottles-suggest.md](../reference/bottles-suggest.md): the guided start.
- ADR 0018: `--auto`, which runs the suggestion without asking.
- PRD 0117 (design repo): suggested bottles.
