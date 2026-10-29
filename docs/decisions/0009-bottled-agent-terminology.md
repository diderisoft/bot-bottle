# ADR 0009: The running thing is a "bottled agent"; "bots" for short

- **Status:** Accepted
- **Date:** 2026-10-29
- **Deciders:** didericis

## Context

The glossary already separated a **bottle** (the policy definition) from a
**bottled agent** (an agent definition running in its bottles under one
identity). The CLI and console didn't: `active`, `stopped`, `resume` and
`commit` talked about "bottles", and the console said "agent" for the running
thing while also listing "agent definitions". "Session" meant three things:
the harness's conversation, the host controller's terminal, and a console
sign-in.

PRD 0110 makes a bottled agent last across stops, holding many harness
conversations, so the overloads would get worse. Names considered: bottle
sessions, agent sessions, bottle instances, agents, bots, runs, genies,
workers. "Session" in any form clashes with the harness's own sessions
(`claude --resume <session>`); "agents" keeps "agent" meaning two things;
"runs" clashes with a run's changes (PRD 0107) and suggests something that
ends.

## Decision

- The thing that runs, stops and resumes is a **bottled agent**, in docs, the
  console and CLI messages. The definition is always an **agent definition**;
  a **bottle** is only the policy.
- The CLI lists them with `bot-bottle bottled-agents`, shorthand **`bots`**
  (and `ba`), with `--running` and `--stopped`. `bots` is something you type,
  never prose.
- To **bottle** an agent is to start one: the console's launch action is
  "Bottle agent".
- **Session** means only the harness's conversation. The host controller's
  per-agent terminal is the **terminal** (`open` / `exited`).
- `active` and `stopped` stay for a release as old names, printing a note;
  `active`'s output is unchanged for scripts. Internal names (`identity`,
  `slug`, the `/bottles` API paths) are unchanged.

## Consequences

- Help, messages, table headings and the glossary say "bottled agent";
  `stopped` and `active` go after the next release.
- PRDs that say "bottle" for the running thing are read with this meaning;
  new ones use "bottled agent".
