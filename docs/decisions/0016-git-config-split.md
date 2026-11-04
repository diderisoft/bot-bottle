# ADR 0016: Split a workspace's git config between the agent and git-gate

- **Status:** Proposed
- **Date:** 2026-11-04
- **Deciders:** didericis

## Context

A copied git workspace brings its `.git` into the bottle. Until #973 that
included the host's `.git/config` as is. A repo config can hold credentials,
like a token in a remote URL (`https://<token>@github.com/…`) or an
`http.extraHeader` with `Authorization`. It can also hold settings that name
host paths or commands, like `core.hooksPath`, `core.sshCommand`, `include`,
and diff and merge drivers.

The sandbox already made such a credential hard to use. HTTPS git push
through the egress proxy is blocked, `Authorization` is stripped on most
routes, and SSH only leaves through git-gate. But the bottle's model is that
the agent never holds a secret it wasn't given, and here it held one as data.

git in a bottle doesn't need the host's remote credentials. It reaches every
granted upstream through git-gate, which holds a token minted for that grant
(ADR 0008, ADR 0014). What git-gate can't do today is use remote-facing
settings a host relies on for a forge bot-bottle doesn't mint tokens for,
such as a self-hosted git server with its own CA, a proxy, or a token in the
remote URL.

## Decision

A repo's config is split by who needs each part:

1. **The agent's copy** gets only settings git needs locally, from an
   allowlist. That covers `core.*` except host paths and commands, branch
   tracking, `remote.*` refspecs, `user.name` and `user.email`, aliases, LFS,
   and plain `insteadOf` rewrites. Remote and submodule URLs lose their user
   info. This is done in #973.
2. **git-gate** is where remote-facing settings would go, for granted repos
   only.
3. **Host-only** settings go nowhere: `include` and `includeIf` paths, and
   `credential.helper` (it names a helper on the host that neither side can
   run).

What's decided now: credentials are stripped, and **host credentials are not
forwarded to git-gate.** A granted repo uses its minted token, as before.

Proposed for later, as an opt-in feature:

- **Non-secret remote settings** move to git-gate's config for that repo, so
  a custom CA (`http.sslCAInfo`) or a proxy keeps working through the gate.
- **Host credentials**, the user info from a remote URL or an `extraHeader`,
  are forwarded only on a per-repo opt-in, and only for forges bot-bottle
  can't mint a token for. A grant's minted token is always preferred.
- **Secrets behind a `credential.helper`** (a keychain, `gh`) stay out of
  scope unless designed separately. Resolving one means running
  `git credential fill` on the host and moving a keychain secret into the
  gateway.

## Consequences

- The agent's repo config holds no host credential, and the launch names
  what it left out (never the value). Nothing in-bottle git does breaks,
  since its upstream traffic already goes through git-gate. A remote URL
  that used to carry a token now matches the gate's `insteadOf` rewrite.
- Some host remote settings stop applying in the bottle until the opt-in
  exists, such as a custom CA or proxy for an unminted forge. For forges
  bot-bottle mints tokens for (Gitea, GitHub), nothing changes.
- When the opt-in exists, a forwarded host token is usually broader than the
  grant, often the whole account with write access. git-gate still enforces
  the grant's read or write at push time, but the credential behind it is
  wider. That's why it's opt-in, per repo, and never preferred over a minted
  token.

## Links

- #812: host `.git/config` credentials copied into bottles
- #973: the allowlisted copy
- ADR 0008: forge-minted keys are grants
- ADR 0014: GitHub HTTPS user tokens for git
- [backends.md: The workspace's repo config](../reference/backends.md#the-workspaces-repo-config)
- [git-gate.md](../reference/git-gate.md)
