# ADR 0023: Plain SSH servers are forges; bottle files hold no long-form repos

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** didericis

Amends [ADR 0008](0008-forge-minted-keys-are-grants.md) and
[ADR 0019](0019-forges-are-described-in-bottles-connected-on-hosts.md).

## Context

A bottle reaches a repo in one of two ways. A **grant**
(`gitea.example.com/acme/app:write`) names a forge the bottle declares, and
the host's connection to that forge supplies the credential. The **long
form** spells the repo out in the bottle file:

```yaml
git_gate:
  repos:
    lib:
      url: ssh://git@git.example.com/org/lib.git
      key: {provider: static, path: /host/keys/lib}
      host_key: ssh-ed25519 AAAA...
```

ADR 0008 kept the long form only for `provider: static`, an operator's own
key file, for servers bot-bottle has no forge kind for. It's SSH only.

The long form is now the one place a bottle file holds something that only
means something on one machine: a key file's path. Elsewhere bottles describe
and hosts connect (ADR 0019). With the bottle library (PRD 0131) syncing
bottle files to every host, a static key path is wrong on every host but one.

It also leaves two rules where there could be one. The long form must be
refused for a minted key (`gitea`, `github`) and allowed for `static`. The
library's parse checks, on the host and in the console, checked the shape
but not that rule, so a bottle file could sync and then fail every launch.

## Decision

**A plain SSH git server is a forge, of kind `ssh`.** It's declared like any
forge and its repos are grants:

```yaml
git_gate:
  forges:
    git.example.com:
      kind: ssh                    # an SSH git server: no API, no minted keys
      ssh: ssh://git@git.example.com:2222
      host_key: ssh-ed25519 AAAA...
  repos:
    - git.example.com/org/lib:write
```

**A grant on an `ssh` forge names the repo's path on the server,
verbatim.** Plain SSH servers keep repos at any path (`/srv/git/app.git`,
`/throwaway.git`), so on an `ssh` forge only, everything after the forge's
name is that path, one segment or more:
`git.example.com/srv/git/app.git:write` is
`ssh://<user>@git.example.com[:port]/srv/git/app.git`, and
`git.example.com/throwaway.git` is `/throwaway.git`.

- Nothing is added or taken off, so `.git` is in the URL exactly when the
  grant has it.
- Each segment uses the characters an owner or repo name may (letters,
  digits, `.`, `_`, `-`); none is empty, `.` or `..`.
- The gate's local name is the last segment without a trailing `.git`. Two
  grants with the same one need the mapping form to name them apart, as
  for any forge.
- A gitea or github grant stays exactly `<owner>/<repo>`.

**The host's connection to an `ssh` forge holds its keys.** A key is either
for one repo (a deploy key) or the forge's default (a user key that reaches
many repos):

```sh
bot-bottle forge connect ssh://git@git.example.com:2222   # pins the host key, or --host-key
bot-bottle forge key add git.example.com org/lib ~/.ssh/lib_deploy --write
bot-bottle forge key add git.example.com --default ~/.ssh/id_ed25519
bot-bottle forge key list|remove …
```

- A grant uses its repo's key, else the forge's default. With neither, the
  launch is refused, naming the `forge key add` that fixes it. A repo's
  key is keyed by the same verbatim path as its grants: `forge key add
  <forge> <path> <key file>`, where `<path>` is what the grant names after
  the forge.
- **A key is stored by reference:** its path and its fingerprint, never a
  copy. Rotating a key in place is picked up; a changed fingerprint is used
  and logged once, and `forge key list` shows it. A missing file fails the
  launch with the fix. A key file readable by group or others is refused, as
  ssh refuses it, and so is one owned by another user or protected by a
  passphrase (git-gate can't type one). The fingerprint is of the public key
  derived from the private one, never a `.pub` beside it, which may be
  stale. A launch's error names the grant and the fix, never the file's
  path: launch errors reach the console and the agent.
- **Two forges may point at the same key file** (a machine-wide key). Nothing
  ties a key to one forge; removing it from one forge only drops that
  reference.
- **Read or write is stated when the key is added** (`--write`), since there's
  no API to ask. git-gate still enforces each grant's read or write at push
  time. A key that can do less than its grant fails at the server's push.
- **Trust names keys by fingerprint.** Trusting a project bottle records the
  server's address and pinned host key and the fingerprints of the keys its
  repos use (ADR 0019's trust covers the connection). A rotated key asks
  again, at the launch that will use it.
- Keys stay on the host. Under ADR 0021 the host reports, per `ssh` forge,
  which repos it holds keys for, read or write, and each key's fingerprint:
  names only. The console's repo picker lists those repos (it can't ask the
  server), shows when two forges share a key, and a launch on a host without
  the key says which key is missing there.

**Bottle files hold no long-form repos.** Every `git_gate.repos` entry in a
bottle file is a grant. A long-form entry, whatever its key provider, fails
to load with the grant it becomes and the commands that connect it. The long
form stays only as what a grant expands into after loading, for git-gate and
everything after it. `provider: static` goes away with it; its connection is
an `ssh` forge's key.

`bottles migrate` rewrites a bottle's static entries: a forge declaration per
server (from the `url` and `host_key`) and a grant per repo, naming its
path from the `url` verbatim, whatever its depth, so no static repo is
left to rewrite by hand (short of a path whose characters no grant can
name). Where the long form's name differs from the one the grant gives,
the mapping form keeps it. It never
connects a server or adds a key: a server declared but not connected here,
or holding no key for a repo, leaves the bottle not loading on this host,
its error naming `forge connect` (with the bottle's pinned host key) and
`forge key add`.

## Consequences

- No bottle file holds a host path or a credential. A bottle with an `ssh`
  forge means the same on every host, and runs on any host that has the key.
- One rule for every check, the launch, the host's parse check and the
  console's: a bottle file's repos are grants.
- **Lost on `ssh` forges:** anything that needs a forge API. `create-repo`
  and `actions.create` refuse an `ssh` forge, and the repo picker lists only
  repos some host holds a key for.
- A grant on an `ssh` forge isn't always `<owner>/<repo>`: anything that
  shows or matches grants (the console's load check and repo picker
  among them) takes a path of any depth there.
- **Read or write isn't checked against the server.** A `--write` key that
  can only read fails on the agent's first push, not at launch.
- Existing static bottles stop loading until migrated. The error lists every
  such repo and the commands, as ADR 0008's did.
- HTTPS to a server bot-bottle has no forge kind for is still not covered.
  ADR 0016's proposed opt-in (forwarding a host's remote settings and
  credentials to git-gate) would add it as a connection's transport, not as
  a long form.

## Links

- [ADR 0008](0008-forge-minted-keys-are-grants.md): forge-minted keys are grants
- [ADR 0016](0016-git-config-split.md): the proposed opt-in for host credentials
- [ADR 0019](0019-forges-are-described-in-bottles-connected-on-hosts.md): forges are described in bottles, connected on hosts
- [ADR 0021](0021-forge-connections-belong-to-the-account.md): connections belong to the account
- [git-gate reference](../reference/git-gate.md)
- PRD 0131 (the bottle library), in the design repo
- #1082
