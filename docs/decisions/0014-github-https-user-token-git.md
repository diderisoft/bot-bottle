# ADR 0014: GitHub repos go through git-gate over HTTPS with the user's token

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** didericis

## Context

ADR 0012 connects github.com through the shared `bot-bottle` GitHub App's
user access tokens. git-gate first reached GitHub repos the way it reaches
Gitea ones: a deploy key minted per repo per launch, revoked at teardown.

Deploy keys have one real strength: GitHub itself holds each run to one repo,
read-only unless the grant writes. They cost a great deal for it. Creating
them needs the app's **Administration: read & write**, which covers a repo's
settings and collaborators, not just its keys. The user token the host keeps
then carries that power on every repo the app is installed on. And an org can
turn deploy keys off (GitHub answers "Deploy keys are disabled for this
repository"), which blocked the first real grant on dev.

## Decision

- **HTTPS with the user's token is the default for GitHub.** git-gate fetches
  and pushes `https://github.com/<owner>/<repo>.git` with it; nothing is
  minted. The app needs only **Contents: read & write** and **Metadata: read**.
- **Deploy keys stay, opt-in per forge** (`transport: deploy-key`), for where
  GitHub-side per-repo scope is worth Administration. Gitea keeps deploy keys.
- **The token is in the gateway's memory only.** The host copies it into
  tmpfs in the gateway (0600, through a temp file, never a command line),
  renews it ahead of expiry and copies the new one in, and removes it at
  teardown. The hooks pass it to git through environment variables in a
  subshell: never in argv, repo config or a bottle.
- **git-gate holds each bottle to its grants.** One namespace per bottle, and
  `git-gate.readOnly` for a grant without `:write`, as before.

## Consequences

- A user token can't be narrowed per repo: it reaches every repo the app is
  installed on. A compromised gateway could push to any of them while it
  holds a token (at most eight hours), not just one repo. This is the cost
  accepted for dropping Administration, which is the larger risk: the host's
  token is no longer an admin token anywhere.
- Installing the app on selected repos, not all, is how an operator keeps
  that reach small; the docs say so.
- Org policies on deploy keys no longer matter for the default path.
- Existing GitHub forge entries without `transport` move to HTTPS on their
  next launch; their deploy-key permission can then be removed from the app.

## Links

- ADR 0012 (the app and its user tokens; its deploy-key transport is
  replaced by this record).
- [git-gate.md, GitHub transports](../reference/git-gate.md#github-transports).
- PRD 0112 (forge security model, design repo) left "deploy keys or HTTPS
  with a token" open; this answers it for GitHub.
