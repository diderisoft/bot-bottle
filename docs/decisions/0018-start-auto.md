# ADR 0018: `bot-bottle start --auto` runs the suggested bottle without asking

Syntax in this record predates v0.1.9; see [manifest.md](../reference/manifest.md).

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** didericis

## Context

The guided start shows the suggested bottle and asks before running it.
Some users want the speed of YOLO mode (just run the agent), but with a
boundary sized to the task rather than none. Asking every time is the
friction that pushes people towards a wide-open bottle.

## Decision

`--auto` skips the confirm step and runs the suggestion as it is.

- It prints one line saying what it chose: the bottles it reuses or stacks
  and any additions (`--auto: running in basic, adding egress
  api.open-meteo.com (from architect)`). The bottles used are already in
  the run's audit, and an unsaved one's full text with them (ADR 0017), so
  there's no separate record.
- It doesn't save the bottle (ADR 0017).
- Anything the bottle missed goes through the existing supervise approvals
  at runtime. Whether an approval persists stays the user's choice there.
- `BOT_BOTTLE_START_AUTO=1` makes it the default; `--no-auto` asks again for
  one run.
- Without a terminal nothing prompts, so `--auto` is the only way to use a
  suggestion there, and the task must come as an argument: `bot-bottle start
  --auto "fix the flaky test"`.

## Consequences

- Speed without an unbounded agent: the suggestion is validated like any
  bottle (redundant and invalid elements dropped, no `inspect: false`, no
  secret values), and the gateway still enforces it.
- A bad suggestion runs without review. The mitigations are its narrow
  defaults (the architect biases toward too little access), the one-line
  summary, the audit, and supervise approvals for what it missed.

## Links

- ADR 0017: suggested bottles aren't saved unless asked.
- [bottles-suggest.md](../reference/bottles-suggest.md).
- PRD 0117 (design repo): suggested bottles.
