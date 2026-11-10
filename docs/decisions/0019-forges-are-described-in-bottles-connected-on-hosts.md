# ADR 0019: Forges are described in bottles and connected on hosts

Syntax in this record predates v0.1.9; see [manifest.md](../reference/manifest.md).

- **Status:** Accepted
- **Date:** 2026-11-06
- **Deciders:** didericis

> **Amended 2026-11-10.** [ADR 0020](0020-where-host-state-lives.md) moves
> `forges.md` into the host database (`forge_connections`, `forge_users`).

## Context

A repo grant (`gitea.example.com/acme/app:write`) loads only when a bottle in
its chain declares the forge (ADR 0008). `bot-bottle forge connect` writes
that declaration, with the forge's token and pinned host key, into a forge
bottle (`gitea`, `github`), and other bottles reach the forge by extending it.

That couples two things. To grant one repo, a bottle has to stack the forge
bottle, and with it everything the forge bottle carries: its other repos, its
egress, its token route. The bottle architect stacks `gitea` just to make a
forge reachable, and the run then looks like it chose `gitea`. That is how a
supervise "keep" offered to write an unrelated route into the shared `gitea`
bottle (#988).

Moving forges out of bottles entirely, into a host-only list, would fix the
stacking but break a property bottles have: a project bottle describes what
it needs and travels with the project. It would also make a grant's prefix a
host-local name, so the same committed grant could resolve to a different
forge on another machine.

What can't travel was already host-local: the token's value (in
`~/.bot-bottle/secrets`) and how this machine reaches the forge (an SSH
address over a VPN, say).

## Decision

**A grant's forge is its real host.** The prefix of
`<host>/<owner>/<repo>[:write]` is the forge's hostname, never an alias, so
the grant's text fixes which forge it means on every machine.

**Bottles describe forges; hosts connect them.**

- A bottle's `git-gate.forges.<host>` says what the forge is: `kind`, and
  `url`/`ssh` when they aren't `https://<host>` and `ssh://git@<host>`.
  `<host>` must be the `url`'s host. It may pin `host_key`; it never carries
  a token, a user or a transport. A bottle with `token:` in a forge fails to
  load, pointing at `forge connect`.
- `~/.bot-bottle/forges.md`, written only by `bot-bottle forge connect`, holds
  what this machine connected: the forge's `kind`, the `url` and `ssh` it
  uses here (`--ssh` for a VPN address), the pinned `host_key`, and its
  `users`, one marked `default`. Each user has its token's secret name and,
  on GitHub, its `transport` (and, for github.com, its `app`). A user's name
  is what the forge reports the token belongs to.
- That file is the host's connection source for now. A launch asks the
  source, not the file, so connections could come from elsewhere later
  without bottles changing.

**A launch uses the host's connection.** A grant on a forge the host hasn't
connected fails to load, naming the `forge connect` command (with the URL
from a bottle's description when it has one). Where a bottle describes the
forge too, its `kind` must match the host's, and its `host_key`, if pinned,
must match the host's pin. Its `url` and `ssh` aren't compared: they are the
hint for connecting it elsewhere, and the host's own are what get used. A
token is sent only to the URL its host connected, so no bottle can route it
somewhere else.

**No forge bottle is needed.** A grant needs no forge declared in its chain.
The bottle architect, the rules path and the runnability check stop stacking
forge bottles; a bottle they write carries the forge's description itself.
Forge bottles that hold other things stay ordinary bottles.

**The token's owner picks GitHub's transport.** `https` puts the user's token
in the gateway for the run; `deploy-key` puts only a key per repo there, but
minting one needs admin access to the repo (the app's Administration
permission, or a classic `repo` token from a repo admin). Which a token can
do isn't reliably knowable before trying, and neither is strictly safer, so
whoever connects the user chooses, and a bottle can't. A token that can't mint
fails the launch at minting, saying so.

**The agent picks the account.** An agent's `forge_users: {<host>: <user>}`
names which connected user it pushes as; without it, the forge's `default`
user (or its only one). Grants name no user, so they stay portable.

**Trust covers the connection.** Trusting a project bottle records, for each
forge its grants use, this machine's connection: `kind`, `url`, `ssh`,
`host_key`, and the default user with its `transport`. When any of them
changes, the bottle asks to be trusted again, showing what changed. Each machine compares
only against itself. Host bottles don't re-prompt: the bottle and the
connection are both yours.

**No migration.** A forge bottle written by an earlier `forge connect`
(`token:` in its forge) fails to load until the forge is connected again with
`forge connect`, which writes `forges.md`, and the token line is removed.

## Consequences

- Granting a repo brings in nothing else. A suggested or project bottle is
  self-contained: its grants and its forge's description, with only the
  connection left to the machine.
- A grant means the same forge everywhere. A machine's connection decides
  only how it gets there and as whom.
- A project bottle can't send your token to a server of its choosing: its
  forge description can't redirect the connection.
- A forge's description is repeated in each bottle that carries one. It
  changes rarely, and a stale one can only fail to connect, not reach
  another forge.
- A host key rotation is one `forge connect` per machine, plus a re-trust of
  project bottles that use the forge. Bottles that pinned the old key need
  editing; pinning in a bottle stays optional for that reason.
- Each machine connects each forge once before bottles that use it run, as
  each needs its tokens today.
- This replaces ADR 0008's "a bottle in the chain declares the forge" and its
  forge bottles; its rule that forge-minted keys are declared only as grants
  stands.

## Links

- [ADR 0008](0008-forge-minted-keys-are-grants.md): forge-minted keys are
  declared only as grants.
- [ADR 0017](0017-suggested-bottles-unsaved-unless-asked.md): suggested
  bottles aren't saved unless you ask.
- [git-gate reference](../reference/git-gate.md#forges-and-one-line-grants).
- #988: "keep" targeting a stacked forge bottle.
- PRD 0103 (design repo): forge bottles and one-line grants.
