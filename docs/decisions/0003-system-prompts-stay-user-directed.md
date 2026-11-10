# ADR 0003: Keep agent system prompts user-directed, not auto-generated from config

- **Status:** Accepted
- **Date:** 2026-05-29
- **Deciders:** didericis

## Context

A bottle already declares exactly what an agent can reach: egress routes
(allowlisted hosts + auth) and git config (remotes + identity). We
considered deriving an agent's system prompt — or a section of it —
automatically from those configs, so an agent would be told up front
what it has access to (e.g. "you can reach `git.example.com` over the
git remote and its API"). The question surfaced while hand-writing that
exact line into the `claude-implementer` prompt.

## Decision

System prompts stay **user-directed** — authored by the operator. We do
not auto-generate prompt content from a bottle's egress / git config.

## Consequences

- The operator controls what the agent is *told* about its environment,
  independently of what the bottle *grants*. Sometimes we may want to
  withhold that information from the agent directly — keep the prompt
  silent about an allowlisted host even though egress permits it.
- The agent can still infer its access on its own (attempt a request,
  read its env, `git remote -v`, the gitconfig), so auto-injection is a
  convenience, not a capability the agent depends on.
- Cost accepted: operators must restate access in the prompt when they
  want the agent to know it (as we did for the Gitea instance), and the
  prompt can drift from the config. That decoupling of "what the bottle
  grants" from "what the agent is told" is the point.
- Revisit if keeping prompts in sync with configs becomes a real pain.
  An *opt-in* helper that emits a capability summary the operator
  chooses to include would honor this decision; silent auto-injection
  would not.

## Amendment (2026-10-26): fixed operating notes

Agents kept failing to reach git repos: they allowed a host for egress and
expected git to follow, not knowing that git always goes through git-gate. So
bot-bottle now appends one fixed section to every agent's prompt
(`contract/agent_notes.py`): how egress and git-gate work in any bottle. It is
the same text for every bottle and says nothing derived from the bottle's
config (no hosts, no repos), so it doesn't tell the agent what it was granted,
and the decision above still holds for that.

## Links

- ADR 0002 (`0002-agent-identity-claimed-not-vouched.md`) — related
  agent-trust posture (what the agent is granted vs. what it can claim).
