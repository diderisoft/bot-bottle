# ADR 0008: A forge-minted key is declared only as a grant

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** didericis

## Context

PRD 0103 added forges to bottles: a forge is declared once (API URL, the
credential its token comes from, SSH address, pinned host key) and shared
through `extends`. A repo on it is then a single line,
`<forge>/<owner>/<repo>[:write]`. The older long form stayed valid alongside
it: a `git-gate.repos` entry with its own `url`, a `key` block
(`provider: gitea`, `forge_token_env`, `api_url`) and a `host_key`.

That meant one forge's settings could be repeated in each repo that used it,
and then drift from the forge entry. A rotated token, a new SSH address or a
re-pinned host key changes the forge but not those copies. Tools that wrote
bottles (keeping a run's grants, the console's manifest views) could produce
either form, and the long form lost which forge a repo belonged to.

## Decision

A repo whose key a forge mints (`key.provider: gitea`, and any forge kind
added later) is declared only as a grant on a forge in the bottle's chain.
A bottle file with a long-form entry for such a key fails to load. The error
names the repo and the grant form.

The long form stays valid for `provider: static`: an operator-supplied key
file. That covers forges bot-bottle doesn't support yet.

Internally nothing changes. Each grant still expands, while `extends` is
resolved, into the long form that the git-gate and everything after loading
read.

## Consequences

- A forge's settings live in one place, and every repo on it follows them.
- Everything that writes bottles writes grants. A repo granted during a run
  with no grant string on record can't be kept or copied into a bottle; it
  has to be granted again.
- Existing bottle files with long-form gitea repos stop loading until they
  are rewritten as grants. (Later: the load error lists every such repo
  and the fix, and `bot-bottle bottles migrate` rewrites the file once the
  forge is connected; see the git-gate reference.)
- Adding a forge kind means adding it to `FORGE_KINDS`, which then also
  refuses that kind's long form.

## Links

- PRD 0103 (design repo): forge
  bottles and one-line grants.
- [git-gate reference](../reference/git-gate.md).
