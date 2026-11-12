# ADR 0021: Forge connections belong to the account; every bottle declares its forges

- **Status:** Accepted
- **Date:** 2026-11-12
- **Deciders:** didericis

Amends [ADR 0019](0019-forges-are-described-in-bottles-connected-on-hosts.md).

## Context

ADR 0019 split forges in two: bottles describe a forge (`git_gate.forges`:
kind, url, ssh, host key), hosts connect it (the token, the user, the
transport). It also let a grant load with no forge declared anywhere in its
bottle's chain, because declaring one then meant stacking a whole forge
bottle, with its other repos, routes and token (#988).

Two things now hurt:

- **The console has no forges of its own.** Connections exist only on each
  host. Building a bottle in the console means typing `forge/owner/repo` by
  hand, with no list of forges or repos, and Write is offered on repos the
  account can only read. Whether a forge works depends on which host the
  bottle later launches on, which the person building it shouldn't have to
  track.
- **Bottles can lean on the host.** A bottle whose grants have no declared
  forge says nothing about which forge it means beyond a hostname. On another
  machine there's no description to connect from.

Tokens can't simply move to the console. The console would become the most
valuable target (push access to every account's repos), it would reverse
"the console can set a secret but never read one", and for GitHub it doesn't
work at all: an expiring user token's refresh token rotates on every
refresh, so two holders of one authorization lock each other out.

## Decision

**A forge connection is the account's.** The console keeps, per account, the
connections its hosts report: forge, kind, url, ssh, the forge login, and for
GitHub the App and where it's installed. **Never a token.** Each host that
holds the token for a connection is a *carrier* of it.

- When the console needs the forge's API (listing repos and their
  permissions for the repo picker), it asks a carrier, which answers with
  names and permissions only.
- Hosts appear in one place: launching on a host that doesn't carry a forge
  the bottle needs, where the console offers to connect it there.
- A connection's credential has a *source*. Today it's the host. An
  end-to-end encrypted vault, which the console can't read, may become a
  second source for static tokens. GitHub App connections stay on hosts,
  each host with its own authorization, because of refresh rotation. Until
  there's a vault, a static token (Gitea) is entered on each host that needs
  it; tokens are not copied host to host, which would need the vault's key
  trust without its benefits.

**Every runnable bottle declares the forge of each grant.** A grant's forge
must be declared somewhere in the bottle's resolved chain. This reverses
0019's "a grant needs no forge declared in its chain": a declaration is now a
few lines (kind, url) that the console, the bottle architect, supervise
`add-repo` and `bottles migrate` write, so the cost that motivated it is gone.

- A bottle with a grant on an undeclared forge is **partial**. It loads, can
  be shown and extended, and can't be launched alone; the error names the
  forge and the fix.
- `bot-bottle bottles migrate` adds missing declarations from the host's
  connections, and the console does the same from the account's. Neither
  invents a forge: with no connection known, the bottle stays partial.
- Everything else in 0019 stands: grants name the forge's real host, a
  declaration never holds a token, user or transport, the launch uses the
  host's own url and token, and trust covers the connection.

## Consequences

- The console gets an account-level Forges list, a repo picker with
  permissions, and launch-time connects, without holding a credential.
- Existing bottles with bare grants become partial until migrated. Project
  bottles in repos need the migration committed.
- A new host still needs each forge connected once (GitHub: a device code,
  the App install is already the account's; Gitea: the token, until a
  vault).
- Repo names, including private ones, reach the console when the picker
  lists them. They're the account's own, cached briefly and not logged.

## Links

- [ADR 0019](0019-forges-are-described-in-bottles-connected-on-hosts.md),
  amended here; [ADR 0012](0012-github-app-user-tokens.md) (GitHub App user
  tokens); [ADR 0020](0020-where-host-state-lives.md) (where host state lives).
- [git-gate.md](../reference/git-gate.md#forges-and-one-line-grants),
  [manifest.md](../reference/manifest.md).
- PRD 0130 (design repo): the feature this decision enables. PRD 0114
  (design repo): the end-to-end encrypted vault.
