# ADR 0004: Risk-weighted coverage, not a single global target

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** didericis
- **Revised:** 2026-07-27 — thresholds relaxed (critical minimum 90→85%,
  diff-coverage gate 90→80%) to cut low-value test churn on changed lines.
  The risk-weighting structure and the "global is informational" rule are
  unchanged.

## Context

bot-bottle is a security tool: it sandboxes agents, scans egress for
secret exfiltration, strips credentials, and gates git pushes. A latent
bug in that logic is expensive, so test coverage there genuinely
matters. But the repo also contains code where coverage is a poor
signal:

- **Interactive entry-point shells** — `cli/init.py` (a `read_tty_line()`
  prompt loop) and `cli/tui.py` (a curses picker). Their bodies are I/O;
  a unit test has to fake the entire terminal conversation, so it
  inflates the number without asserting behaviour that would otherwise
  go unchecked.
- **Subprocess / backend orchestration** — the docker / smolmachines /
  macos-container backends shell out to `docker`, `container`, `smolvm`.
  Mock-heavy unit tests here mostly re-assert the argv you already
  wrote (the test passes whether or not the real teardown works), while
  many of the missed *branches* are failure paths you cannot provoke
  against a real daemon on cue.

Chasing a single global percentage (e.g. 90%) pushes the most test
effort onto the least safety-relevant code — exactly backwards — and
invites performative tests written to colour a line rather than to catch
a regression (Goodhart's law).

## Decision

Coverage is **risk-weighted**, measured over the **combined unit +
integration** suites, with three rules:

1. **Critical modules must remain ≥ 85%.** The curated security/logic core
   covers the host and gateway egress policy, manifest trust boundary,
   git-gate enforcement, supervise protocol/server, YAML parser, and bottle
   state. The concrete module list lives in `scripts/critical-modules.txt`;
   `scripts/critical_modules.py` rejects stale or ambiguous entries before
   Coverage.py can silently ignore them. These modules are unit-testable, so
   CI enforces the aggregate minimum independently of diff coverage.

2. **Subprocess/backend orchestration is covered by the integration
   suite, not omitted.** `scripts/coverage.sh` runs unit + integration
   under one coverage measurement so these modules are scored where they
   are actually exercised. They stay *visible* — hiding the code that
   tears down sandboxes and wires networks is the one place we will not
   omit.

3. **Interactive entry-point shells are omitted** (`.coveragerc`), with a
   rationale comment. This is the only sanctioned use of `omit` besides
   `tests/*`.

The forward-looking guard is a **diff-coverage gate**
(`scripts/diff_coverage.py`): new/changed executable lines on a branch
must be ≥ 80% covered. This catches regressions where they are
introduced without forcing a back-fill crusade through legacy glue. The
gate skips lines in omitted files (there is no coverage data for them),
so the omit list cannot launder *new* logic into the dark: anything that
needs real testing must live outside the interactive shells to be
scored at all.

The **global percentage is informational**, not a CI gate — it would
otherwise be hostage to the CI runner's Docker availability and to the
omit list.

## Consequences

- The number we report (`scripts/coverage.sh`) means "coverage of the
  code we consider testable, across both suites" — a dip is a real
  regression in code we control, not noise from added CLI glue.
- No incentive to write mock-the-mock tests for orchestration to defend
  a global figure.
- The omit list needs governance: an entry must be a genuinely
  interactive shell, justified in the `.coveragerc` comment and here.
  `cli/init.py` and `cli/tui.py` qualify; backend orchestration does
  not.
- CI must run the integration suite under coverage to score the
  orchestration modules; where the runner lacks Docker those tests skip
  and their modules read low — accepted, because the *enforced* gates
  (critical-module standard + diff coverage) are Docker-independent.
- "We're at N%" is now a curated figure; outsiders should read the
  policy, not a single number.
- A rename or removal in the curated list fails CI. Updating the list is an
  explicit review of where the security-critical behavior moved, not a way to
  improve the percentage by omission.

## Links

- PRs #290 (cover the egress adapter), and the coverage-policy PR that
  introduces this record.
- `.coveragerc`, `scripts/coverage.sh`, `scripts/diff_coverage.py`.
- `scripts/critical-modules.txt` — the single source of truth for the
  core-module list, read by `scripts/coverage.sh`.
- The README's coverage badges and the `update-badges.yml` workflow that
  refreshed them were removed in 2026-10 (#712): the workflow had failed for
  months, and the gates above are what CI enforces.
