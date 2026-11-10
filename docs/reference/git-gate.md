# git-gate

git-gate is the gateway's git server. Agents push and fetch through it and
never hold an upstream credential.

| | What git-gate does |
|---|---|
| Push | scans the push, then forwards it upstream over SSH, or over HTTPS with the forge's token for a GitHub repo |
| Fetch | refreshes from upstream first, so the agent sees current state. A fetch is refused if upstream can't be reached. |

HTTPS is the default for GitHub (see [GitHub transports](#github-transports)).

The start of each push refreshes from upstream too. So its secret scan covers
only the commits upstream doesn't have. The gateway's repos start empty after
it restarts.

If upstream can't be reached, a push is still scanned, over more history.

## Manifest

A bottle's `git_gate` block sets the agent's git identity, the push checks and
the repos.

```yaml
git_gate:
  user:                       # optional: the agent's git identity
    name: implementer-bot
    email: bot@example.com
  trailers: require           # optional: require (default) | warn | off
  protect_default_branch: true  # optional: refuse deleting or rewriting it (default off)
  actions:                    # optional: what the agent may do on a forge without a card
    create: [github.com/acme/*]   # create-repo under acme, no card (below)
  repos:                      # local name: the gate's bare repo
    bot-bottle: gitea.example.com/org/bot-bottle:write   # a grant on a forge (below)
    lib:
      url: ssh://git@git.example.com/org/lib.git
      key:
        provider: static
        path: /host/keys/lib  # a key file already on the host
      host_key: ssh-ed25519 AAAA...   # pins the upstream host key
```

A long-form entry's `url` must be `ssh://user@host[:port]/path`
(`manifest/git.py`). A one-line grant names no URL. Its forge connection
decides how git-gate reaches it.

| Key provider | Key | Declared as |
|---|---|---|
| `static` | a key file already on the host | the long form above. It's for repos on a forge bot-bottle doesn't support yet. |
| `gitea` | a fresh ed25519 deploy key, added through the forge API at launch and revoked at teardown | only a [grant on a forge](#forges-and-one-line-grants) |

For `gitea` keys:

- A key is read-only unless the grant ends in `:write`.
- Keys last one launch. A resume or relaunch mints its own.
- It first revokes any key a killed previous run left registered. The forge
  refuses a second key with the same title.
- The title is `bot-bottle:<hostname>[-<environment>]-<label>:<repo>`. The
  environment is named outside `live`.
- The code is `git_gate/provision.py` and
  `contrib/gitea/deploy_key_provisioner.py`.
- A bottle file with a long-form `provider: gitea` repo fails to load
  ([ADR 0008](../decisions/0008-forge-minted-keys-are-grants.md)).

See [Migrating from the old format](#migrating-from-the-old-format) for such
files.

**Default branch.** `protect_default_branch: true` makes git-gate refuse a push
that deletes or rewrites each repo's default branch upstream. Rewriting means a
non-fast-forward. See step 6 of [Push](#push).

- It's for forges without branch protection.
- It applies to every repo in the bottle, including ones added later with
  `add-repo`.
- It's off by default, because agents rebase their own branches and some work
  on the default branch of a scratch repo.
- A later bottle's value wins, so a child can turn off a parent's.

**Create actions.** `actions.create` lists where the agent's `create-repo` is
answered without asking (`manifest/git.py`).

| Pattern | Covers |
|---|---|
| `<forge>/<owner>/*` | any repo under an owner |
| `<forge>/<owner>/<name>` | one repo |

Lists from every bottle are joined. A request anywhere else gets a card (see
[Created repos](#created-repos)).

**Host keys.** Without a `host_key`, an interactive launch fetches one with
`ssh-keyscan`. It asks the operator to confirm it, and can save it to the
bottle file (`git_gate/host_key.py`).

A headless launch without a `host_key` refuses to start.

### Forges and one-line grants

A bottle grants repos on a forge in one line each, and may describe the forge
they're on. The host connects it
([ADR 0019](../decisions/0019-forges-are-described-in-bottles-connected-on-hosts.md)).

```yaml
# any bottle
extends: basic
git_gate:
  forges:                             # optional: what to connect elsewhere
    gitea.example.com:                # the forge's host
      kind: gitea                     # url and ssh only when not the defaults
  repos:
    - gitea.example.com/acme/app:write
    - gitea.example.com/acme/lib      # read-only
```

A grant's prefix is the forge's real host. So a committed grant means the same
forge on every machine. The code is `manifest/forges.py` and
`forge_connections.py`.

A description says what the forge is:

- `kind`
- `url` and `ssh`, when they aren't `https://<host>` and `ssh://git@<host>`
- optionally a pinned `host_key`

A description never carries a token, a user or a transport. A bottle with
`credentials:`, or with `token`, `account`, `app` or `transport` in a forge,
fails to load, pointing at `forge connect`.

No bottle needs to describe a forge for its grants to load.

#### Host connections

What a launch uses is the host's connection, in `~/.bot-bottle/forges.md`.
Only `bot-bottle forge connect` writes that file.

```yaml
gitea.example.com:
  kind: gitea
  url: https://gitea.example.com
  ssh: ssh://git@10.0.4.2:30009       # how this machine reaches it (--ssh)
  host_key: ssh-ed25519 AAAA...       # pinned when connected
  users:
    alice:
      token: secret:GITEA_EXAMPLE_COM_TOKEN # a name in ~/.bot-bottle/secrets
      default: true
    codex:
      token: secret:CODEX_GITEA_EXAMPLE_COM_TOKEN
```

- A grant on a forge the host hasn't connected fails to load. The error names
  the `forge connect` command, with a description's URL when the chain has one.
- Where the chain describes the forge too, its `kind` must be the
  connection's. A pinned `host_key` must be the connection's pin.
- The description's `url` and `ssh` aren't compared. They're the hint for
  connecting it elsewhere.
- The connection's `url` and `ssh` are what's used. So a bottle can't send a
  token anywhere the host didn't connect.
- A launch asks `forge_connections.connections()`, the host's connection
  source, not the file.

#### Forge users

A launch pushes as the forge's `default` user unless the agent names another
with `identity.accounts`. A forge with one user pushes as that one.

```yaml
# an agent
identity:
  accounts:
    gitea.example.com: codex
```

A forge's user is the account the forge says its token belongs to.
`identity.accounts` is among manifest.md's agent fields.

#### Connecting a forge

`bot-bottle forge connect <url> [--default] [--ssh URL] [--transport T]`
records a connection (`cli/commands/forge.py`, `forge_connect.py`).

1. It recognizes the forge's kind (`forge_kinds.py`, table below).
2. It takes a token, pasted without echo or given with `--token-stdin`.
3. It asks the forge whose token it is and which of its repos it's admin on.
   It warns when none, because deploy keys need admin.
4. It pins the SSH host key it finds for the forge's repos, without asking.
   It fails when it finds none.
5. It stores the token in the host secrets file and records the user in
   `forges.md`.

| Forge | Recognized by |
|---|---|
| github.com | `github`, without asking |
| Gitea | `/api/v1/version`, five seconds |
| GitHub Enterprise Server | `/api/v3/meta`, five seconds |
| any other | refused, naming the kinds it takes |

The forge's SSH address must be reachable from the host, which a public name
may not be. Give it with `--ssh`, or the card's SSH field.

| User | Secret name |
|---|---|
| a forge's first user | `<HOST>_TOKEN` |
| another user | `<USER>_<HOST>_TOKEN` |

A user connected again keeps its name. The first user is the default. Another
becomes it with `--default`.

Connecting again replaces the forge's `url`, `ssh` and pin.

The CLI and the console's card both say the token each kind needs:

| kind | token page | permissions |
|---|---|---|
| `gitea` | `<url>/user/settings/applications` | scopes `write:repository` and `read:user`, from an account that's admin on the repos |
| `github` | `<url>/settings/personal-access-tokens/new` | a fine-grained token for the repos with Contents: read (read & write to push) and Metadata: read. Administration: read & write too, only for a user connected with `--transport deploy-key`. A classic token with `repo` also works. |

#### Deploy keys

Gitea always reaches its repos with deploy keys minted per launch. So does a
GitHub user connected with `--transport deploy-key`. A GitHub forge otherwise
uses HTTPS with its user's token ([Transports](#github-transports)).

Each such launch:

1. revokes the keys a previous run left registered
2. mints an ed25519 deploy key on each granted repo with
   `POST /repos/{owner}/{repo}/keys`, read-only unless `:write`
3. revokes each key at teardown

The API is Gitea's under `/api/v1`, or GitHub's at `api.github.com` or a
server's `/api/v3`. The code is `contrib/<kind>/deploy_key_provisioner.py`.

A repo's key and its revocation record name the kind, as `provider`.

#### github.com through the bot-bottle GitHub App

github.com connects through the shared `bot-bottle` GitHub App's user access
tokens (ADR 0012). A pasted personal token is the fallback, with `--token`.

1. The operator installs the app on the accounts and orgs whose repos bottles
   will grant, at `https://github.com/apps/<slug>/installations/new`.
2. The operator authorizes the host with GitHub's device flow.
   `bot-bottle forge connect github` prints a code to enter at
   `https://github.com/login/device`. The console's host page (Forges) shows it.

The token acts as the operator, as "<user> via bot-bottle". It works only:

- on repos the app is installed on and the operator can reach
- with the app's permissions: Contents: read & write and Metadata: read
- plus Administration: read & write, only for a user connected with
  `--transport deploy-key`

For other users git-gate fetches and pushes with the token over HTTPS
([Transports](#github-transports)).

**The app.** The app is named by its slug and public client id. The default is
`bot-bottle`. `--app` and `--client-id` set it, and it's kept in
`<root>/github-app.json`.

So each named environment can use its own app. Dev uses `bot-bottle-dev`.

No private key or client secret of the app is ever on a host or the console.
The device flow and refreshing its tokens take the client id alone
(`contrib/github/user_token.py`).

**The token.** The token goes to the host secrets file under the user's secret
name. The user is recorded on `github.com` in `forges.md` with `app: <slug>`
(`github_connect.py`). `--default` makes it the default user.

An expiring token lasts eight hours, the app's default.

- It also keeps `_REFRESH`, `_EXPIRES` and `_CLIENT_ID` companions.
- Before it's used to mint or revoke a key, it's renewed under a lock when
  under ten minutes remain.
- The rotated refresh token lasts six months and replaces the old one.
- When it can't be renewed, the error says to connect GitHub again.

An agent's `add-repo` card for a repo the token can't reach carries the app's
install link.

**Installing** is part of connecting.

- After authorizing, `forge connect github` lists the app's installations the
  token reaches. On a host already connected it does this straight away.
- `--reauthorize` authorizes again.
- With no installation, or none on `--owner <account>`, it opens the install
  page.
- It then asks GitHub every five seconds, for up to ten minutes, until the
  installation appears.
- It says where the app is installed: "all repos" or how many selected.
- `--no-wait` only prints the install link.

The console's host page shows the same two steps. While an `add-repo` card for
an unreachable repo is shown, the host asks GitHub again every ten seconds, for
fifteen minutes.

Once the repo is reachable, the host raises the card again with the grant
options.

#### GitHub transports

A GitHub repo is reached over `https` by default, or over `deploy-key`, chosen
per connected user (ADR 0014).

| | `https` (the default) | `deploy-key` |
|---|---|---|
| How | git-gate fetches and pushes `https://github.com/<owner>/<repo>.git` with the forge's token | a key per repo per launch, as for Gitea |
| Minted | nothing | a key per repo per launch |
| Held to granted repos by | git-gate | GitHub itself, per repo |
| Cost | the token reaches every repo the app is installed on | needs admin on the repo, the far broader Administration permission |

The transport is set with `forge connect --transport`, as `transport:` on the
user in `forges.md`. The app's device flow takes it too.

On a host already connected, `forge connect github --transport T` changes just
the transport. Whoever connects the user chooses. A bottle can't (ADR 0019).

**`https`.** The host copies the current token into the gateway's tmpfs, as
`/dev/shm/git-gate/<bottle_id>/<SECRET>-token`.

- The file is 0600.
- tmpfs is mounted on `/dev/shm` if it isn't one. Provisioning refuses to go
  on without it.
- The copy goes through a 0600 temp file, never a command line.
- On macos-container it's streamed in over `container exec -i`'s stdin.
  `container cp` lands beneath a tmpfs mount, unseen.
- Teardown removes it.

Every four minutes the host controller renews the token within ten minutes of
expiry. It copies the new one in when it changed
(`control/hostctl/github_git_tokens.py`, `backend/provision_gateway.push_tokens`).

The hooks read the token per git call. They pass it to git as an
`Authorization` header through `GIT_CONFIG_*` environment variables in a
subshell. It's never in argv, a repo's config or a bottle.

A 401, 403 or 404 from GitHub says what to do. That is to authorize again, give
the app Contents: read & write, or install it on the repo's owner.

Beside the token the host writes its expiry, as `<SECRET>-token.expires` in
epoch seconds. So a 401 tells two cases apart:

| 401 | Means | Fix |
|---|---|---|
| after the expiry | the host controller hasn't renewed the token | start it again with `bot-bottle up` |
| before the expiry | GitHub refused it, so the authorization was revoked | reconnect GitHub |

An authorization the renewer can't renew raises a console alert instead of a
warning every pass. So does a Gitea token the forge refuses. The alerts are
`forge_alerts` (see host-controller.md's `host_stats`).

**`deploy-key`.** Minting needs admin access to the repo. That is the app's
Administration: read & write, or a classic `repo` token from a repo admin.

- A token that can't mint fails the launch at minting, saying so.
- Use it where GitHub itself should hold each run to one repo.
- To use both on one host, connect a second user with `--transport deploy-key`.
  Pick it with an agent's `identity.accounts`.

**The trade-off.** A user token can't be narrowed per repo, so it reaches every
repo the app is installed on.

git-gate is what holds each bottle to its granted repos. It uses one namespace
per bottle, and `git-gate.readOnly` for a grant without `:write`.

The gateway holds the token only while the bottle runs. Install the app on
selected repos to keep its reach small.

Deploy keys keep GitHub-side per-repo scope. But they need the far broader
Administration permission, which the host's token then carries too. An org can
also turn them off.

#### How a grant loads

While `extends` is resolved, each grant becomes a long-form entry. That's what
everything after loading sees:

- the connection's SSH address and pinned host key
- a key minted per launch with the user's token, or on GitHub over `https`,
  the token itself
- `read_only`, unless the grant ends in `:write`

A mapping names the gate repo: `repos: {site: gitea.example.com/acme/app}`.
The list form names it after the repo.

Descriptions merge by host through `extends`, a child's winning. Two parents
describing one forge differently is a load error.

A read-only repo is marked `git-gate.readOnly` in its bare repo. The
pre-receive hook refuses pushes to it before scanning, saying a `:write` grant
is needed.

The trust prompt shows each repo as `fetch` or `push`, and where its key is
minted with which secret. Trusting a project bottle also records this host's
connection to each forge its repos are on (see manifest.md's Trust).

## Agent side

The agent's `~/.gitconfig` sends each repo's upstream URL to the gate
(`control/git_gate_render.py`).

- An `insteadOf` rewrite maps each upstream URL to
  `http://<gateway>:9420/<name>.git`.
- An `http.extraHeader` sends `x-bot-bottle-identity: <token>`, scoped to the
  gate's URL.

The header is written even for a bottle without repos, since every bottle is
given the gate's address. So a repo granted later only adds a rewrite.

A bottle launched before that gets the header when it's first granted a repo.
It's taken from its agent's proxy URL, `http://bottle:<token>@<gateway>:<port>`,
whose host is the gate's on every backend.

The agent's env also points `core.hooksPath` at the commit hooks (see
[Commit trailers](#commit-trailers)).

A copied workspace's own repo config carries no credentials
([backends.md](backends.md#the-workspaces-repo-config)). Its remote URLs lose
their user info, and `http.*` and `credential.*` stay on the host.

Upstream credentials live only in the gate. A remote URL that had a token in
it then matches the gate's `insteadOf` rewrite, so it goes through the gate
like any other.

### The only push path

The gate is the only way to push, unless a bottle grants a `passthrough: true`
route to a forge, or a forge API route with gateway auth that can write to
repos (see [gateway.md](gateway.md)).

The agent's `NO_PROXY` covers the gateway, so git traffic to it skips the
egress proxy. Smart-HTTP push to a forge through the egress proxy is blocked on
inspected routes.

## Commit trailers

Every commit an agent makes carries a trailer naming the bottle and run, and
git-gate checks pushes for it. This is part of the core: it works without the
console.

```
Bot-Bottle: <bottle>/<run>
Co-Authored-By: <operator name> <operator email>
```

The trailer is a visible label that git-gate verifies at push time, not proof.
Anyone can write one into a commit made elsewhere.

The authoritative record of what a bottle pushed is git-gate's audit journal.
It lists every forwarded commit with its bottle and push time (see
[The push record](#the-push-record)).

| Part | Value |
|---|---|
| `<bottle>` | the bottles the launch was built from, joined with `+`: the `--bottle`s, else the agent's `bottle:` |
| `<run>` | the bottled agent's identity, its slug, e.g. `implementer-k3x9q` |
| `Co-Authored-By` | the operator: the host's global git identity, `git config --global user.name`/`user.email` where the launch runs |

- `<run>` is minted at start and kept across stop and resume. So commits made
  before a stop still push after a resume.
- `<bottle>` and `<run>` are reduced to `[A-Za-z0-9._-]`
  (`contract/git_trailers.py`, `backend/resolve_common.py:launch_trailer`).
- `Co-Authored-By` is left out when either name or email is unset. It's also
  left out when the commit's author is the operator, by the same email.
- Harnesses add their own model trailers. bot-bottle adds none, and doesn't
  change the author.

### Adding trailers

A `prepare-commit-msg` hook adds the trailers in every repo
(`git_gate/commit_hooks.py`).

- The agent's env sets `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`
  to `core.hooksPath=~/.config/git/bot-bottle-hooks`.
- Those entries come after any `GIT_CONFIG_*` entries the bottle's env has.
- Env config overrides a repo's own `core.hooksPath`, such as bot-bottle's
  `.githooks`. So the hooks run in every repo.
- `provision_git` fills the directory with one dispatcher linked under every
  hook name, and `bot-bottle.gitconfig` holding the values.
- `push-to-checkout` and `proc-receive` get no link, because their presence
  alone changes what git does.

The dispatcher runs the hook git would have run without the override, with the
same args, stdin and exit code. So a repo's own hooks still run and can still
reject.

That hook is the `core.hooksPath` from the repo's and user's config, else
`<common dir>/hooks/<name>`.

As `prepare-commit-msg`, once the repo's hook succeeds, the dispatcher adds the
trailers with `git interpret-trailers`:

- `Bot-Bottle` replaces an existing one.
- `Co-Authored-By` is added only if not already there.

So amending never duplicates them. `prepare-commit-msg` runs for merges and
amends too, and isn't skipped by `--no-verify`.

If the hooks can't be installed, launch warns and carries on.

### Checking trailers

Before the gitleaks scan, pre-receive checks the trailer on every commit the
push introduces (`gateway/git_gate/trailer_check.py`).

That is every commit reached by neither a ref the gate already has from
upstream nor the bottle's [baseline](#the-baseline). Merge commits made in the
bottle are checked.

A commit passes when it has a `Bot-Bottle` trailer and every one it has equals
this bottle's value.

A failing push is rejected, never rewritten, since rewriting would change the
agent's hashes. The message lists the commits and the command that fixes them,
e.g.

```
git rebase --rebase-merges --exec 'git -c trailer.ifexists=replace commit --amend --no-edit --allow-empty --trailer "Bot-Bottle: dev/implementer-k3x9q"' 1a2b3c4d5e6f
```

`git_gate.trailers` in the bottle sets the mode:

| Mode | Does |
|---|---|
| `require` (the default) | refuses the push |
| `warn` | forwards the push and prints the same message |
| `off` | doesn't check |

Like other `git_gate` fields it merges through `extends`, a child's winning.

The value and mode are kept in the bottle's git-gate state dir, as
`trailer.json`, for re-provisioning. They're set on each bare repo as
`git-gate.trailer` and `git-gate.trailers`.

### The baseline

A copied workspace can hold commits the operator made but never pushed. Those
push untouched.

Every launch writes the bottle's baseline to the git-gate state dir, as
`baseline-commits`, one SHA a line.

- When the workspace is a git repo copied in, the baseline is
  `git rev-list --all --not --remotes` on the host copy
  (`git_gate/service.py:workspace_baseline`).
- That is the commits its local refs reach and its remote-tracking refs don't.
- Otherwise the baseline is empty.
- A resume adds the host repo's new unpushed commits to it.

Commits a remote-tracking ref reaches are left out to keep the file small.
They're upstream, which the gate already exempts.

Provisioning copies the baseline to `/git-gate/creds/<bottle_id>/baseline-commits`
and names it in each bare repo's `git-gate.baseline`.

The check excludes those commits and everything they reach. It uses
`git log --stdin --ignore-missing`, so a baseline commit the push doesn't carry
is skipped.

A baseline commit the agent rewrites, by amending or rebasing, is a new commit
and is checked. Commits the bottle brings in from elsewhere, such as another
repo, still need the trailer, or a `warn` bottle.

### Misconfiguration

A repo with missing trailer config refuses every push, whatever the mode.

Provisioning always sets `git-gate.trailers`, `git-gate.trailer` and
`git-gate.baseline` on every repo. A repo missing any of them, or whose
baseline file isn't there, refuses with:

```
<key> is missing or invalid; provisioning may have failed, or this bottle predates commit trailers. Relaunch the bottle to reprovision it.
```

Relaunching the bottle reprovisions the repo config and baseline.

### The push record

Each ref git-gate forwards is a `forge.push_accepted` audit event
(`gateway/git_gate/audit.py`).

| Payload field | Holds |
|---|---|
| `commits` | every commit the push brings to the gate, oldest first: those reachable from the new tip and no ref the gate had |
| `commit_count` | the number of commits |
| `bot_bottle_trailer` | the repo's trailer value |
| `commits_truncated` | set when one ref has more than 10,000 commits. They're counted and the list truncated. |

Baseline commits are listed too. This is what was forwarded, whether or not the
trailer check looked at it.

The orchestrator relays the event named by the bottle's slug, with the registry
id as `activation`. The event's time is the push time.

Under `audit.delivery: required` the record is written before the ref is
forwarded. The ref is refused if it can't be.

git-gate doesn't write git notes. Pushing `refs/notes/*` upstream would add a
ref the forge has to accept for every push.

## Gateway side

One process serves every bottle, each from its own namespace
(`gateway/git_gate/http_backend.py`).

- It reads a request body declared by Content-Length or sent chunked, up to
  100 MiB within 30 seconds. Git sends a pack bigger than `http.postBuffer`
  chunked.
- It resolves the bottle from (source IP, identity header) through the
  orchestrator. An unattributed or unreachable lookup returns 404.
- It serves `git http-backend` from that bottle's namespace, `/git/<bottle_id>/`.
- Credentials are in `/git-gate/creds/<bottle_id>/`.

Keeping bottles in separate directories is what keeps one bottle's push key out
of another's repos.

At registration, `backend/provision_gateway.py` copies a bottle's keys and
known_hosts into the running gateway and inits its bare repos.

### Live grants

An approved `add-repo` adds a repo to a running bottle
(`git_gate/live_grant.py`, `control/hostctl/repo_grants.py`).

The forge is this host's connection to it, as the user the session's agent
pushes as. That's its `identity.accounts`, else the forge's default. A forge the host
hasn't connected can't be granted.

1. The grant records the key in the bottle's git-gate state dir, as
   `live-grants.json`.
2. It mints the key and adds the repo to the upstream snapshot.
3. Re-provisioning the gateway then creates the repo there.
4. `git config --add` in the guest adds its `insteadOf` rewrite.

Keeping the grant writes it into the bottle with the forge's description.

- Only forge grants can be added this way.
- Teardown revokes live-granted keys with the launch's own.
- On a gateway restart, they are provisioned again (see
  [backends.md](backends.md#gateway-restarts)).

### Created repos

An agent's `create-repo` asks for a new, empty repo, `<forge>/<owner>/<name>`.
It's private unless the agent asks for a public one
(`control/hostctl/repo_create.py`).

| Case | What happens |
|---|---|
| The forge isn't connected here | refused before any card. The agent is told to `forge-connect`. |
| The repo exists already | refused before any card. The agent is told to `add-repo`. |
| The GitHub App isn't ready on the repo's owner | the card waits (below) |
| The bottle's `actions.create` allows it | the host creates it at once |
| Otherwise | the console shows a card saying what will be created, as whom, and that it's then granted with write |

Through the GitHub App, the repo's owner must have the app installed with
Administration (write) accepted. Until then:

- The card carries `install:`, the install page or the installation's settings
  to accept the permission.
- Allowing it leaves it waiting.
- The host asks GitHub again, as for an unreachable `add-repo` (see
  [host-controller](host-controller.md)). It raises the card again once that's
  fixed.

The repo is created with the forge's API as the user the agent pushes as there.
It goes under that user, or under an org the request names.

Then it's granted to the running bottle with write, as a
[live grant](#live-grants). The token stays on the host.

A repo created but not granted is reported so. The agent can `add-repo` it.

### Fetch

A fetch refreshes from upstream first, and fails if upstream is unreachable.

The access hook runs before every `upload-pack`. It runs
`git fetch origin --prune` against the upstream.

It uses the repo's key and pinned known_hosts, or HTTPS with the token for a
GitHub repo ([Transports](#github-transports)). If the upstream is unreachable,
the fetch fails instead of serving stale data.

### Push

The pre-receive hook (`git_gate_render_hook`) reads every ref first, then:

1. Rejects Gitea AGit review refs: `refs/for/*`, `refs/draft/*`,
   `refs/for-review/*`. Push a branch and open the PR from it instead.
2. Checks every new commit carries this bottle's `Bot-Bottle` trailer. See
   [Commit trailers](#commit-trailers).
3. Runs gitleaks over the commits the push introduces, with rules from
   `git_gate_render_gitleaks_config`. A finding refuses the push.
4. Re-scans ignoring `# gitleaks:allow` comments. A finding hidden only by an
   inline suppression becomes a `gitleaks-allow` supervise proposal.
5. Scans for the pushing bottle's own secrets, by value (below).
6. With `protect_default_branch`, refuses deleting or force-pushing the
   upstream's default branch (below).
7. Forwards each accepted ref to `origin` over SSH, checking the pinned host
   key, or over HTTPS with the token for a GitHub repo.

The hook scans every ref before forwarding any. So a hit on one ref can't leave
earlier refs half-pushed.

Each decision is an audit event from the `git-gate` producer. Under
`audit.delivery: required`, a ref whose record can't be written is refused.

#### Details

**Step 3.** The commits the push introduces are those reachable from the new
tip and not from any ref the gate already has.

**Step 4.** The push waits for the operator. It's refused on rejection or
timeout.

**Step 5** is `gateway/git_gate/held_secret_scan.py`. It looks in every ref
name, commit message, identity, path and added line the push introduces.

The bottle's own secrets are:

- its secret env values of 8 or more characters: an `env:` entry written
  `secret:NAME`, `secret:?<prompt>` or `secret:${VAR}`
- its egress tokens
- its canary

It matches by value, so it catches secrets gitleaks has no rule for.

**Step 6** is set by `git-gate.protectDefault` on the gate repo
(`control/git_gate_protect.py`).

- The default branch is the upstream's `HEAD`.
- It's asked of the upstream with `ls-remote --symref`, only when a push
  deletes or rewrites a branch.
- If the upstream doesn't say, the push is refused.
- Other branches can still be rewritten and deleted.

## Migrating from the old format

Before forges, each repo with a forge-minted key carried its own copy of the
forge's settings. A bottle written like this no longer loads.

```yaml
git-gate:
  repos:
    app:
      url: ssh://git@10.0.0.5:30009/acme/app.git
      host_key: ssh-ed25519 AAAA...
      key: { provider: gitea, forge_token_env: GITEA_ADMIN_TOKEN, api_url: https://gitea.example.com }
```

The error lists every such repo in the bottle. It says whether this host has
connected the forge, and shows the replacement (`manifest/legacy_git_gate.py`).

1. Connect the forge once, unless the error says it's connected already:
   `bot-bottle forge connect https://gitea.example.com`.
2. Run `bot-bottle bottles migrate <bottle>` (`manifest/migrate.py`).

When the repos' SSH address isn't `ssh://git@<forge host>`, the error's command
adds `--ssh` with the address they used. Here it's an IP and port.

| Old | Becomes |
|---|---|
| a repo with `provider: gitea` | its grant. Nothing is added to `extends`. |
| the `api_url`'s host | the forge. Without an `api_url`, the upstream's host. |
| the SSH URL's path | the owner and repo |
| a key without `read_only: true` | a grant ending in `:write` |
| a key with `read_only: true` | a grant without `:write` |
| `host_key` and `forge_token_env` | removed. The grant uses the connection's pinned host key and its user's token. |

- `migrate` notes when the connection's host key or token differ from the old
  ones, or the connection pins no host key.
- Grants are written as a list when every repo converts and is named after its
  repo. Otherwise they're a mapping that keeps the gate's names and any
  `static` repos.
- Only those lines change, and the `git-gate` key becomes `git_gate`. Old
  egress forms are converted too, their block written afresh
  ([manifest.md](manifest.md#old-forms)). Comments, `env` and the rest stay.
- The result is parsed back to check that before it's written.
- It shows the diff and asks first. `--yes` doesn't ask, and without a
  terminal `--yes` is needed.
- A host bottle's migration is a version, after any hand edits are versioned.
  `bot-bottle bottles revert <bottle> <version>` undoes it.
- A project bottle's migration is left for the project to commit.
- With the forge not connected, `migrate` refuses with the same steps.
  Connecting takes a token typed by the operator, so it never connects one.
- A repo whose SSH path isn't `<owner>/<repo>`, or a file in flow style, is
  left to edit by hand.

A forge bottle an earlier `forge connect` wrote, with `credentials:` and a
`token` in its forge, fails to load too. Connect the forge again with
`forge connect`, which writes `forges.md`.

Then remove the `credentials:` block and the forge's `token` and `account`
lines (ADR 0019).
