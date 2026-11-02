# git-gate

git-gate is the gateway's git server. Agents push and fetch through it and
never hold an upstream credential. Pushes are scanned and forwarded upstream
over SSH. Fetches refresh from upstream first, so the agent sees current
state, and so does the start of each push, so its secret scan covers only
the commits upstream doesn't have (the gateway's repos start empty after it
restarts). A fetch is refused if upstream can't be reached; a push is still
scanned, over more history.

## Manifest

```yaml
git-gate:
  user:                       # optional: the agent's git identity
    name: implementer-bot
    email: bot@example.com
  trailers: require           # optional: require (default) | warn | off
  repos:                      # local name: the gate's bare repo
    bot-bottle: gitea.example.com/org/bot-bottle:write   # a grant on a forge (below)
    lib:
      url: ssh://git@git.example.com/org/lib.git
      key:
        provider: static
        path: /host/keys/lib  # a key file already on the host
      host_key: ssh-ed25519 AAAA...   # pins the upstream host key
```

Only `ssh://user@host[:port]/path` upstreams are supported (`manifest/git.py`).
Key providers:

- `static`: a key file already on the host, in the long form above. This is
  for repos on a forge bot-bottle doesn't support yet.
- `gitea`: a fresh ed25519 deploy key is added through the forge API at
  launch and revoked at teardown (`git_gate/provision.py`,
  `contrib/gitea/deploy_key_provisioner.py`), read-only unless the grant ends
  in `:write`. Keys last one launch: a resume or relaunch mints its own, and
  first revokes any a killed previous run left registered (the forge refuses
  a second key with the same title). The title is
  `bot-bottle:<hostname>[-<environment>]-<label>:<repo>`, the environment
  named outside `live`. It's declared only as a grant on a forge (below): a bottle
  file with a long-form `provider: gitea` repo fails to load
  ([ADR 0008](../decisions/0008-forge-minted-keys-are-grants.md)); see
  [Migrating from the old format](#migrating-from-the-old-format).

Without a `host_key`, an interactive launch fetches one with `ssh-keyscan`,
asks the operator to confirm it, and can save it to the bottle file
(`git_gate/host_key.py`). A headless launch refuses to start.

### Forges and one-line grants

A bottle can declare a forge once and grant repos on it in a line
(`manifest/forges.py`; PRD 0103, design repo).
The forge usually lives in its own bottle that others extend:

```yaml
# ~/.bot-bottle/bottles/gitea.md
credentials:
  gitea-api: { source: secret, secret: GITEA_TOKEN }   # value in ~/.bot-bottle/secrets
git-gate:
  forges:
    gitea.example.com:                # the host, or <account>@<host>
      kind: gitea
      url: https://gitea.example.com
      ssh: ssh://git@gitea.example.com:30009   # default ssh://git@<url host>
      token: $gitea-api
      host_key: ssh-ed25519 AAAA...

# a bottle that extends it
extends: [gitea, basic]
git-gate:
  repos:
    - gitea.example.com/acme/app:write
    - gitea.example.com/acme/lib      # read-only
```

`bot-bottle forge connect <url> [<bottle>] [<name>]` writes the forge bottle
(`cli/commands/forge.py`, `forge_connect.py`). It recognizes the forge's kind
(`forge_kinds.py`): `github` for github.com without asking; otherwise Gitea's
`/api/v1/version` or a GitHub Enterprise Server's `/api/v3/meta`, five seconds
each. Any other forge is refused, naming the kinds it takes. It takes a token
(pasted without echo, or `--token-stdin`) and asks the forge whose it is and
which of its repos it's admin on, warning when none (deploy keys need admin). It pins the SSH host key it finds for the forge's
repos without asking, and fails when it finds none: the forge's SSH address
(`--ssh`, or the card's SSH field) must be reachable from the host, which a
public name may not be. It stores the token in the host secrets file as
`<NAME>_TOKEN`, and writes the credential and the forge entry into `<bottle>`
(default: named after the kind, `gitea` or `github`; `./<name>` writes a
project bottle and trusts it), as a new version. A Gitea forge also gets a
read-only `GET` route for its packages and release downloads; GitHub serves
those from other hosts, which a bottle that needs them routes itself.

The token each kind needs (the CLI and the console's card both say it):

| kind | token page | permissions |
|---|---|---|
| `gitea` | `<url>/user/settings/applications` | scopes `write:repository` and `read:user`, from an account that's admin on the repos |
| `github` | `<url>/settings/personal-access-tokens/new` | a fine-grained token for the repos with Administration: read & write (deploy keys), Contents: read (read & write to push) and Metadata: read; a classic token with `repo` also works |

Per launch both kinds do the same: mint an ed25519 deploy key on each granted
repo (`POST /repos/{owner}/{repo}/keys`, read-only unless `:write`; Gitea's
API under `/api/v1`, GitHub's at `api.github.com` or a server's `/api/v3`),
revoke the keys a previous run left registered first, and revoke each at
teardown (`contrib/<kind>/deploy_key_provisioner.py`). A repo's key and its
revocation record name the kind (`provider`); records from before GitHub are
Gitea's. `<name>` is the forge key: the host by default, or
`<account>@<host>` when the bottle already has the host for another account.
Keys that aren't bare identifiers are written quoted
(`"codex@gitea.example.com":`).

While `extends` is resolved, each grant becomes a long-form entry (what
everything after loading sees): a `gitea` key minted per launch with the forge's token, the forge's host key,
and `read_only` unless the grant ends in `:write`. A mapping
(`repos: {site: gitea.example.com/acme/app}`) names the gate repo; the list
form names it after the repo. Forges and credentials merge by name through
`extends`, a child's winning; two parents declaring one differently is a load
error. A grant on a forge no bottle in the chain declares fails at load.
Only the `secret` credential source exists so far.

A read-only repo is marked `git-gate.readOnly` in its bare repo, and the
pre-receive hook refuses pushes to it before scanning, saying a `:write` grant
is needed. The trust prompt shows each repo as `fetch` or `push`, and where
its key is minted with which secret.

### Migrating from the old format

Before forges, each repo with a forge-minted key carried its own copy of the
forge's settings:

```yaml
git-gate:
  repos:
    app:
      url: ssh://git@10.0.0.5:30009/acme/app.git
      host_key: ssh-ed25519 AAAA...
      key: { provider: gitea, forge_token_env: GITEA_ADMIN_TOKEN, api_url: https://gitea.example.com }
```

A bottle written like this no longer loads. The error lists every such repo
in the bottle, says whether a bottle already declares the forge, and shows the
replacement (`manifest/legacy_git_gate.py`). To convert it:

1. Connect the forge once, unless the error says a bottle already declares it:
   `bot-bottle forge connect https://gitea.example.com`. When the repos' SSH
   address isn't `ssh://git@<forge host>` (here an IP and port), the error's
   command adds `--ssh` with the address they used.
2. `bot-bottle bottles migrate <bottle>` (`manifest/migrate.py`).

`migrate` adds the forge bottle to the front of `extends` (unless the chain
already has the forge), so the bottle's own parents still win, and replaces
each old repo with its grant. The forge is the `api_url`'s host, not the SSH
URL's (the old default, the upstream's host, when there's no `api_url`); the
owner and repo come from the SSH URL's path. A key without `read_only: true`
could push, so its grant ends in `:write`; a `read_only: true` key's doesn't.
When every repo converts and is named after its repo, they're written as a
list, else as a mapping that keeps the gate's names (and any `static` repos).
The repos' `host_key` and `forge_token_env` lines go: the grant uses the
forge's pinned host key and token. `migrate` notes when those differ from the
old ones, or the forge pins no host key.

Only those lines change; comments, `env`, `egress` and the rest stay as they
were, and the result is parsed back to check that before it's written. It
shows the diff and asks first (`--yes` doesn't ask; without a terminal it
needs `--yes`). A host bottle's migration is a version, after any hand edits
are versioned, so `bot-bottle bottles revert <bottle> <version>` undoes it; a
project bottle's is left for the project to commit. With no bottle declaring
the forge, `migrate` refuses with the same steps: connecting takes a token
typed by the operator, so it never connects one itself. A repo whose SSH path
isn't `<owner>/<repo>`, or a file laid out in flow style, is left to edit by
hand.

## Agent side

`control/git_gate_render.py` writes the agent's `~/.gitconfig`:

- an `insteadOf` rewrite from each upstream URL to
  `http://<gateway>:9420/<name>.git`;
- an `http.extraHeader` sending `x-bot-bottle-identity: <token>`, scoped to
  the gate's URL. It's written even for a bottle without repos (every bottle
  is given the gate's address), so a repo granted later only adds a rewrite. A bottle launched before that gets it
  when it's first granted a repo, from its agent's proxy URL
  (`http://bottle:<token>@<gateway>:<port>`), whose host is the gate's on
  every backend.

The agent's env also points `core.hooksPath` at the commit hooks (see
[Commit trailers](#commit-trailers)).

The agent's `NO_PROXY` covers the gateway, so git traffic to it skips the
egress proxy. Smart-HTTP push to a forge through the egress proxy is blocked,
so the gate is the only way to push.

## Commit trailers

Every commit an agent makes carries a trailer naming the bottle and run
(PRD 0112, design repo), and git-gate checks pushes for it. This is part of
the core: it works without the console.

The trailer is a visible label that git-gate verifies at push time, not
proof: anyone can write one into a commit made elsewhere. The authoritative
record of what a bottle pushed is git-gate's audit journal, where every
forwarded commit is listed with its bottle and push time (see
[The push record](#the-push-record)).

```
Bot-Bottle: <bottle>/<run>
Co-Authored-By: <operator name> <operator email>
```

- `<bottle>` is the bottles the launch was built from (`--bottle`s, else the
  agent's `bottle:`), joined with `+`; `<run>` is the bottled agent's
  identity (its slug, e.g. `implementer-k3x9q`), minted at start and kept
  across stop and resume, so commits made before a stop still push after a
  resume. Both are reduced to `[A-Za-z0-9._-]`
  (`contract/git_trailers.py`, `backend/resolve_common.py:launch_trailer`).
- `Co-Authored-By` credits the operator: the host's global git identity
  (`git config --global user.name`/`user.email` where the launch runs). It's
  left out when either is unset, or when the commit's author is the operator
  (same email). Harnesses add their own model trailers; bot-bottle adds
  none, and doesn't change the author.

**Adding them** (`git_gate/commit_hooks.py`). The agent's env sets
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` to
`core.hooksPath=~/.config/git/bot-bottle-hooks`, appended after any
`GIT_CONFIG_*` entries the bottle's env has. Env config overrides a repo's own
`core.hooksPath` (bot-bottle's `.githooks`, say), so the hooks run in every
repo. `provision_git` fills the directory with one dispatcher linked under
every hook name (except `push-to-checkout` and `proc-receive`, whose presence
alone changes what git does) and `bot-bottle.gitconfig` holding the values.
The dispatcher runs the hook git would have run without the override (the
`core.hooksPath` from the repo's and user's config, else
`<common dir>/hooks/<name>`), with the same args, stdin and exit code, so a
repo's own hooks still run and can still reject. As `prepare-commit-msg`, once
the repo's hook succeeds, it adds the trailers with `git interpret-trailers`:
`Bot-Bottle` replaces an existing one, and `Co-Authored-By` is added only if
not already there, so amending never duplicates them. `prepare-commit-msg`
runs for merges and amends too, and isn't skipped by `--no-verify`. If the
hooks can't be installed, launch warns and carries on.

**Checking them** (`gateway/git_gate/trailer_check.py`). Before the gitleaks
scan, pre-receive checks every commit the push introduces that neither a ref
the gate already has (upstream) nor the bottle's baseline (below) reaches.
Merge commits made in the bottle are checked. A commit passes
when it has a `Bot-Bottle` trailer and every one it has equals this bottle's
value. A failing push is rejected, never rewritten (that would change the
agent's hashes); the message lists the commits and the command that fixes
them, e.g.

```
git rebase --rebase-merges --exec 'git -c trailer.ifexists=replace commit --amend --no-edit --allow-empty --trailer "Bot-Bottle: dev/implementer-k3x9q"' 1a2b3c4d5e6f
```

`git-gate.trailers` in the bottle sets the mode: `require` (the default)
refuses, `warn` forwards the push and prints the same message, `off` doesn't
check. Like other `git-gate` fields it merges through `extends`, a child's
winning. The value and mode are kept in the bottle's git-gate state dir
(`trailer.json`) for re-provisioning, and set on each bare repo as
`git-gate.trailer` and `git-gate.trailers`.

**The baseline.** A copied workspace can hold commits the operator made but
never pushed. Those push untouched. Every launch writes the bottle's baseline
to the git-gate state dir (`baseline-commits`, one SHA a line): when the
workspace is a git repo copied in, `git rev-list --all --not --remotes` on
the host copy (the commits its local refs reach and its remote-tracking refs
don't; `git_gate/service.py:workspace_baseline`), and empty otherwise. Commits a remote-tracking ref
reaches are left out to keep the file small; they're upstream, which the gate
already exempts. A resume adds the host repo's new unpushed commits to it.
Provisioning copies it to `/git-gate/creds/<bottle_id>/baseline-commits` and
names it in each bare repo's `git-gate.baseline`; the check excludes those
commits and everything they reach (`git log --stdin --ignore-missing`, so a
baseline commit the push doesn't carry is skipped). A baseline commit the
agent rewrites (amends, rebases) is a new commit and is checked. Commits the
bottle brings in from elsewhere, such as another repo, still need the
trailer, or a `warn` bottle.

**Misconfiguration.** Provisioning always sets `git-gate.trailers`,
`git-gate.trailer` and `git-gate.baseline` on every repo. A repo missing any
of them, or whose baseline file isn't there, refuses every push (whatever the
mode) with `this bottle predates commit trailers (its repo has no valid
<key>); relaunch it`. Bottles launched before commit trailers have to be
relaunched.

### The push record

Each ref git-gate forwards is a `forge.push_accepted` audit event
(`gateway/git_gate/audit.py`). Its payload lists `commits`: every commit the
push brings to the gate (reachable from the new tip and no ref the gate had),
oldest first, with `commit_count` and the repo's `bot_bottle_trailer` value.
Baseline commits are listed too: this is what was
forwarded, whether or not the trailer check looked at it. The orchestrator
relays the event named by the bottle's slug, with the registry id as
`activation`, and the event's time is the push time. Under
`audit.delivery: required` the record is written before the ref is
forwarded, and the ref is refused if it can't be. More than 10,000 commits in
one ref are counted and the list truncated (`commits_truncated`). git-gate
doesn't write git notes: pushing `refs/notes/*` upstream would add a ref the
forge has to accept for every push.

## Gateway side

`gateway/git_gate/http_backend.py` serves every bottle from one process. It
reads a request body declared by Content-Length or sent chunked (git sends a
pack bigger than `http.postBuffer` chunked), up to 100 MiB within 30 seconds:

- It resolves the bottle from (source IP, identity header) through the
  orchestrator. An unattributed or unreachable lookup returns 404.
- It serves `git http-backend` from that bottle's namespace,
  `/git/<bottle_id>/`. Credentials are in `/git-gate/creds/<bottle_id>/`.
  Keeping bottles in separate directories is what keeps one bottle's push
  key out of another's repos.

`backend/provision_gateway.py` copies a bottle's keys and known_hosts into
the running gateway and inits its bare repos at registration.

### Live grants

An approved `add-repo` adds a repo to a running bottle
(`git_gate/live_grant.py`, `control/hostctl/repo_grants.py`;
PRD 0105, design repo). The host finds
the forge in the bottles the session launched with, as their files are now,
else in any host bottle. It records the key in the bottle's git-gate state dir
(`live-grants.json`), mints it, and adds the repo to the upstream snapshot.
Re-provisioning the gateway then creates the repo there, and `git config --add`
in the guest adds its `insteadOf` rewrite. Teardown revokes live-granted keys
with the launch's own. Only forge grants can be added this way. On a gateway
restart, they are provisioned again (see
[backends.md](backends.md#gateway-restarts)).

### Fetch

The access hook runs before every `upload-pack`. It runs
`git fetch origin --prune` against the upstream using the repo's key and
pinned known_hosts. If the upstream is unreachable, the fetch fails instead
of serving stale data.

### Push

The pre-receive hook (`git_gate_render_hook`) reads every ref first, then:

1. Rejects Gitea AGit review refs (`refs/for/*`, `refs/draft/*`,
   `refs/for-review/*`). Push a branch and open the PR from it instead.
2. Checks every new commit carries this bottle's `Bot-Bottle` trailer
   (see [Commit trailers](#commit-trailers)).
3. Runs gitleaks over the commits the push introduces: those reachable from
   the new tip and not from any ref the gate already has
   (PRD 0028, design repo). The rules
   come from `git_gate_render_gitleaks_config`. A finding refuses the push.
4. Re-scans ignoring `# gitleaks:allow` comments. A finding hidden only by an
   inline suppression becomes a `gitleaks-allow` supervise proposal. The push
   waits for the operator and is refused on rejection or timeout.
5. Runs `gateway/git_gate/held_secret_scan.py`, which looks for the pushing
   bottle's own secrets in every ref name, commit message, identity, path and
   added line the push introduces. Those secrets are its `?` env values, its
   egress tokens and its canary. It matches by value, so it catches secrets
   gitleaks has no rule for.
6. Forwards each accepted ref to `origin` over SSH, checking the pinned host
   key.

The hook scans every ref before forwarding any, so a hit on one ref can't
leave earlier refs half-pushed. Each decision is an audit event from the
`git-gate` producer. Under `audit.delivery: required`, a ref whose record
can't be written is refused.
