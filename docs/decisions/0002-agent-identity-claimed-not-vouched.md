# ADR 0002: Agent-set git identity is claimed, not vouched

- **Status:** Accepted
- **Date:** 2026-05-28
- **Deciders:** didericis

## Context

PRD 0027 lifts `git.user` (name/email) to the agent layer, so an agent
file may declare its own commit identity. Agent files can live in
`$CWD/.bot-bottle/agents/` — i.e. they can be supplied by a cloned,
less-trusted repository. That raises the question of whether a
repo-supplied agent setting its own git identity is a security concern,
and whether agent identity should be gated differently for `$CWD`
agents than for `$HOME` agents.

This record exists because the decision is a **trust posture** worth
finding on its own, separate from the feature PRD that introduced it.
The full analysis lives in PRD 0027; the decision is summarized here.

## Decision

Allow agents to set `git.user`, and treat an agent-declared identity as
**claimed, not vouched**. No `$CWD`-vs-`$HOME` gating on the identity
field. `git.remotes` stays bottle-only (home-only).

## Consequences

- A cloned repo's agent file can present any commit author name/email,
  including one that reads like a real person's. This is accepted: git
  authorship is **not a credential** (push auth is the bottle's remote
  key/token), is **already forgeable** from inside the bottle at runtime
  (`git config user.email …`), and was never a trust anchor.
- If attribution integrity ever matters, the answer is commit
  **signing** (SSH/GPG), not the author field — so this decision closes
  no door that was open.
- `git.remotes` is deliberately *not* lifted to the agent layer: it
  carries credentials and host trust (IdentityFile, KnownHostKey) and
  remains a bottle-only, home-only concern.
- Revisit if a future change ever makes commit identity load-bearing
  (e.g. enforced signing keyed on author), at which point gating
  `$CWD`-supplied identities would matter.

## Links

- PRD 0027 (design repo) — full trust analysis and schema.
- Issue #94, PR #95 — the feature this decision was made for.
