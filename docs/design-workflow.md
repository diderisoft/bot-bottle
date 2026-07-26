# Design workflow

How bot-bottle turns discussion into canonical design and then into
implementation without leaving the repository's architecture scattered across
issue and review threads.

The goal is not more documentation. The goal is one discoverable current answer
for every load-bearing design question.

## Sources of truth

Design artifacts have different jobs:

| Artifact | Authority |
|---|---|
| Decision records | Stable system-wide boundaries, policies, and invariants |
| PRDs | The current design for a feature |
| Research notes | Evidence and tradeoff analysis; informative, not normative |
| Issues | Work tracking, open questions, and discussion |
| Pull-request comments | Review history; never the final home of a design decision |

When a discussion changes the design, update the relevant PRD or decision
record before treating the discussion as resolved. A comment may explain why a
decision changed, but future implementers must not need to reconstruct the
decision from a thread.

Avoid duplicating the same rule in several canonical documents. Prefer one
canonical statement and links from dependent documents.

## Choosing the canonical artifact

Use a PRD when the decision describes a feature: its behavior, scope, success
criteria, trust model, implementation slices, and tests.

Use a decision record when the choice is broader than one feature or will
constrain several future features. Examples include state ownership, credential
boundaries, compatibility policy, and what the project does or does not claim
as a security guarantee.

Use a research note when the conclusion depends on comparing external systems,
protocols, or approaches. Promote any resulting project decision into a PRD or
decision record.

## From discussion to implementation

### 1. Open the design discussion

An issue may start with incomplete requirements. Record:

- the problem and desired outcome;
- known security or compatibility constraints;
- the current owner of affected state and credentials;
- related PRDs, decisions, issues, and pull requests;
- open questions that would materially change the implementation.

Do not disguise an unresolved trust-boundary or state-ownership decision as an
implementation detail.

### 2. Draft or update the canonical design

Before substantial implementation, write the feature PRD and update any
system-wide decision it changes.

An active design should make these relationships visible near its top:

```markdown
Status: Draft | Active | Superseded | Retargeted
Depends on: #...
Supersedes: ...
```

Record dependencies only on the dependent document. Do not maintain reverse
`Blocks` lists that can drift as dependent work changes.

For security-sensitive work, state:

- the exact guarantee and explicit non-guarantees;
- trusted and untrusted components;
- who creates each identity or attribution field;
- who owns durable state;
- failure and recovery behavior;
- how the design is tested at its boundaries.

### 3. Resolve review into the repository

When review settles a design-changing question:

1. Update the canonical document in the same pull request.
2. Mark conflicting documents Superseded or Retargeted, or update them.
3. Add or adjust dependency links.
4. Leave a concise resolution comment linking to the canonical change.

A useful resolution comment is:

```text
Resolution: <what was decided>
Canonicalized in: <document/section/commit>
Supersedes: <older statement, if any>
Follow-up: <remaining implementation or question>
```

The resolution is incomplete until the repository reflects it.

### 4. Check design readiness

Implementation may begin when:

- the PRD's material trust, ownership, and compatibility questions are settled;
- dependencies and blockers are explicit;
- the design agrees with current architecture and decision records;
- superseded documents are marked or updated;
- success criteria and boundary tests are concrete;
- remaining open questions can be answered during implementation without
  changing the feature's guarantee or component ownership.

Small exploratory spikes may happen earlier. A spike proves feasibility; it does
not establish a production contract or silently settle the design.

### 5. Implement in ordered slices

Prefer small, independently reviewable slices after the parent design is
accepted. Record the dependency chain explicitly.

Parallel work is safe when slices do not compete for the same unsettled
interface or ownership boundary. If a foundational change will alter the
transport, schema, state owner, or trust domain used by another slice, land the
foundation first.

An implementation pull request should identify:

- the PRD or decision it implements;
- the implementation chunk;
- its base and blockers;
- any design deviation discovered during implementation.

If implementation reveals a load-bearing design change, pause that slice and
update the canonical design. Do not let the code and review thread become an
undocumented replacement for the PRD.

## Dependency and staleness management

### Dependency direction

Write dependencies in terms of contracts, not chronology:

```text
credential provisioning contract
  -> host-controller authentication
    -> privileged host operations
```

If only part of a feature is blocked, say so. For example, a manifest parser may
proceed while that feature's durable audit-storage chunk waits for the canonical
audit schema.

### Superseding documents

Do not silently edit history to make an old design appear to have always said
the new thing. Preserve the rationale, but make current status unmistakable:

```markdown
Status: Superseded
Superseded by: <document>
Reason: <one paragraph>
```

If part of a PRD remains valid, mark it Retargeted and identify which scope moved
elsewhere.

Add a short supersession note near the top explaining what changed, why the old
design is no longer current, and where the current design lives. For a research
note whose original analysis remains useful, preserve that analysis and append
a dated addendum with the newer finding instead of rewriting the note as though
it had always reached the new conclusion.

### Architecture sweeps

After a foundational change, do a targeted architecture sweep before building
more features on it:

1. Identify the concepts the change affects, such as `bot-bottle.db`, host
   controller, orchestrator, audit ownership, or signing key.
2. Search active PRDs, decisions, and open issues for those concepts.
3. Update or supersede contradictory statements.
4. Refresh dependency links and the current architecture summary.
5. Confirm stacked implementation branches still have the correct base.

This is a milestone activity, not a recurring documentation ceremony.

## Pull-request checklist

Use the relevant items in design and implementation pull requests:

- [ ] The canonical PRD or decision is linked.
- [ ] Design-changing review decisions are reflected in-repo.
- [ ] Dependencies and blockers are explicit.
- [ ] State, credential, and trust ownership agree with current architecture.
- [ ] Superseded or retargeted documents are marked.
- [ ] Security guarantees and non-guarantees are precise.
- [ ] Open questions do not change the promised guarantee or ownership model.
- [ ] Implementation deviations updated the canonical design.

## Lightweight maintenance

Automation should enforce document shape, not pretend to understand
architecture. Useful checks include:

- active PRDs contain status and dependency metadata;
- superseded PRDs link to their replacement;
- referenced documents and issues exist;
- implementation pull requests identify their PRD and chunk;
- document filenames and lifecycle states follow repository conventions.

Human review remains responsible for detecting conflicting guarantees or
ownership claims.

The durable rule is simple: **discussion discovers the decision; the repository
records it; implementation follows it.**
