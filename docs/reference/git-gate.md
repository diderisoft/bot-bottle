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
  forges:                     # what each grant's forge is (below)
    gitea.example.com: {kind: gitea}
    git.example.com:          # a plain SSH git server (below)
      kind: ssh
      ssh: ssh://git@git.example.com:2222
  repos:                      # local name: the gate's bare repo
    bot-bottle: gitea.example.com/org/bot-bottle:write   # a grant on a forge (below)
    lib: git.example.com/org/lib                         # read-only
```

The commit author is set per part. Each part comes from the first of these
that sets it:

1. the agent's `identity.git`
2. the bottle's `git_gate.user`
3. the agent's name. An agent named `claude` gets `claude` and
   `claude@bot-bottle.invalid`.

So a first commit works in a bottle with no `git_gate`, such as `basic`. A
repo-local identity in the copied workspace still wins, because git reads it
before the global one (`agent_provider.git_identity`).

Every repo in a bottle file is a one-line grant, which names no URL or key.
Its forge connection decides how git-gate reaches it
([ADR 0023](../decisions/0023-ssh-servers-are-forges.md)).

| Forge kind | Key |
|---|---|
| `gitea` (and `github` with `--transport deploy-key`) | a fresh ed25519 deploy key, added through the forge API at launch and revoked at teardown |
| `github` | none: its user's token over HTTPS ([Transports](#github-transports)) |
| `ssh` | a key file this host holds for the server ([SSH servers](#ssh-servers)) |

A bottle file with a repo written out in long form, with its own `url`, `key`
and `host_key`, fails to load, whatever its key: `gitea`, `github`, `ssh`, or
the old `static` key file. The error lists each such repo, the grant it
becomes and the commands that connect it ([Migrating](#migrating-from-the-old-format)).
The long form is only what a grant expands into after loading
(`manifest/git.py`, [How a grant loads](#how-a-grant-loads)).

For `gitea` keys (see also [Deploy keys](#deploy-keys)):

- Keys last one launch. A resume or relaunch mints its own.
- The forge refuses a second key with the same title, so a launch first
  revokes any key a killed previous run left registered.
- The title is `bot-bottle:<hostname>[-<environment>]-<label>:<repo>`. The
  environment is named outside `live`.
- The code is `git_gate/provision.py` and
  `contrib/gitea/deploy_key_provisioner.py`.
- A bottle file with a long-form repo fails to load
  ([ADR 0008](../decisions/0008-forge-minted-keys-are-grants.md),
  [ADR 0023](../decisions/0023-ssh-servers-are-forges.md),
  [migrating](#migrating-from-the-old-format)).

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
[Created repos](#created-repos)). A pattern on a forge the chain declares as
an SSH server fails to load: creating a repo needs a forge API.

**Host keys.** A repo's host key is its forge connection's pin; `forge
connect` pins one. Should a connection have none, an interactive launch
fetches one with `ssh-keyscan` and asks the operator to confirm it, for that
launch only (`git_gate/host_key.py`). A headless launch refuses to start,
saying to connect the forge again.

### Forges and one-line grants

A bottle grants repos on a forge in one line each, and declares the forge
they're on. The host connects it
([ADR 0019](../decisions/0019-forges-are-described-in-bottles-connected-on-hosts.md),
[ADR 0021](../decisions/0021-forge-connections-belong-to-the-account.md)).

```yaml
# any bottle
extends: basic
git_gate:
  forges:                             # required for the grants' forges
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

**A grant's forge must be declared** somewhere in the bottle's resolved
chain: the bottle itself, a parent, or another bottle launched with it. A
bottle whose grants use an undeclared forge still loads and can be extended,
but it's *partial*: a launch of it alone is refused, naming the forge, and
`bot-bottle bottles` lists it as `partial: declares no forge for <host>`.
`bot-bottle bottles migrate <name>` adds the missing declarations from this
host's connections; with no connection for a forge, it says to connect it
first rather than invent one. The bottle architect, supervise `add-repo` and
the console write the declaration with the grant.

#### Host connections

What a launch uses is the host's connection, kept in the host database.
`bot-bottle forge list` and `forge show` print them.

```
$ bot-bottle forge list
gitea.example.com            gitea   alice*, codex
github.com                   github  octo*

$ bot-bottle forge show gitea.example.com
gitea.example.com: gitea at https://gitea.example.com
  ssh       ssh://git@10.0.4.2:30009
  host key  ssh-ed25519 AAAA...
  user      alice (token secret:BB_HOST_FORGE_GITEA_EXAMPLE_COM__ALICE_TOKEN, default)
  user      codex (token secret:BB_HOST_FORGE_GITEA_EXAMPLE_COM__CODEX_TOKEN)
```

- `*` marks a forge's default user. A token is a name in the host secrets
  file, never its value.
- `ssh` is how this machine reaches the forge (`--ssh`). The host key was
  pinned when it was connected.
- Only `forge connect` and the console's connect write a connection. The host
  controller is the only writer (ADR 0020). The CLI goes through it.
- `bot-bottle forge remove <host> [--user U]` disconnects one user, or the
  whole forge. It drops their tokens from the secrets file too.

At launch:

- A grant on a forge the host hasn't connected fails to load. The error names
  the `forge connect` command, with a description's URL when the chain has one.
- Where the chain describes the forge too, its `kind` must be the
  connection's. A pinned `host_key` must be the connection's pin.
- The description's `url` and `ssh` aren't compared. They're the hint for
  connecting it elsewhere.
- The connection's `url` and `ssh` are what's used. So a bottle can't send a
  token anywhere the host didn't connect.
- Connections are the host database's `forge_connections` and `forge_users`
  tables, read through `forge_connections.connections()`.

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
5. It stores the token in the host secrets file and records the user in the
   host database.

| Forge | Recognized by |
|---|---|
| github.com | `github`, without asking |
| Gitea | `/api/v1/version`, five seconds |
| GitHub Enterprise Server | `/api/v3/meta`, five seconds |
| `ssh://user@host[:port]` | an SSH server, kind `ssh` ([SSH servers](#ssh-servers)) |
| any other | refused, naming the kinds it takes |

The forge's SSH address must be reachable from the host, which a public name
may not be. Give it with `--ssh`, or the card's SSH field.

Each user's token is kept as `BB_HOST_FORGE_<HOST>__<USER>_TOKEN`, a name no
bottle can read ([secrets.md](secrets.md#reserved-names)). A user connected
again keeps its name. The first user is the default. Another
becomes it with `--default`.

Connecting again replaces the forge's `url`, `ssh` and pin.

The CLI and the console's card both say the token each kind needs:

| kind | token page | permissions |
|---|---|---|
| `gitea` | `<url>/user/settings/applications` | scopes `write:repository` and `read:user`, from an account that's admin on the repos |
| `github` | `<url>/settings/personal-access-tokens/new` | a fine-grained token for the repos with Contents: read (read & write to push) and Metadata: read. Administration: read & write too, only for a user connected with `--transport deploy-key`. A classic token with `repo` also works. |

#### SSH servers

A plain SSH git server is a forge of kind `ssh`
([ADR 0023](../decisions/0023-ssh-servers-are-forges.md)). It's declared like
any forge, and its repos are grants:

```yaml
git_gate:
  forges:
    git.example.com:
      kind: ssh                     # an SSH git server: no API, no minted keys
      ssh: ssh://git@git.example.com:2222   # when it isn't ssh://git@<host>
      host_key: ssh-ed25519 AAAA...  # optional, as for any forge
  repos:
    - git.example.com/org/lib:write
```

It's named by its host, without the port, which goes in `ssh`. It has no
`url`, and a description with one fails to load.

A grant on an SSH server names the repo's path on the server, verbatim,
after the forge's name. Plain servers keep repos anywhere, so it isn't held
to `<owner>/<repo>` as a Gitea or GitHub grant is:

| Grant | Upstream | Local name |
|---|---|---|
| `git.example.com/org/lib` | `ssh://git@git.example.com:2222/org/lib` | `lib` |
| `git.example.com/srv/git/app.git:write` | `ssh://git@git.example.com:2222/srv/git/app.git` | `app` |
| `git.example.com/throwaway.git` | `ssh://git@git.example.com:2222/throwaway.git` | `throwaway` |

- The path is one or more segments of letters, digits, `.`, `_` and `-`.
  No segment is empty, `.` or `..`, and it has no leading or trailing `/`.
- Nothing is added or taken off: `.git` is in the URL only when the grant
  has it, and `org/lib` and `org/lib.git` are two grants, each with its own
  key.
- The gate's local name is the last segment without a trailing `.git`.
  Two grants with the same one need the mapping form to name them apart,
  as for any forge.
- A Gitea or GitHub grant is still exactly `<forge>/<owner>/<repo>` (a
  trailing `.git` dropped). Any other path on a forge the bottle declares
  as one, or that this host connected as one, fails to load.

The host connects it and holds its keys (`cli/commands/forge_key.py`,
`forge_keys.py`):

```sh
bot-bottle forge connect ssh://git@git.example.com:2222        # pins the host key
bot-bottle forge key add git.example.com org/lib ~/.ssh/lib_deploy --write
bot-bottle forge key add git.example.com --default ~/.ssh/id_ed25519
bot-bottle forge key list [git.example.com]
bot-bottle forge key remove git.example.com org/lib     # or --default
```

- `connect` pins the host key `ssh-keyscan` finds, as connecting any forge
  does, or the one `--host-key '<type> <base64>'` gives (a server this host
  can't reach yet). Connecting again re-pins and keeps the keys. A forge
  connected as another kind has to be removed first.
- A key is for one repo (a deploy key) or the forge's default (a user key
  that reaches many repos). A grant uses its repo's key, else the default.
  A repo's key is added under the path its grants name, exactly as they
  name it: `forge key add git.example.com srv/git/app.git <path>` for
  `git.example.com/srv/git/app.git`.
- A key is kept by reference in the host database (`forge_keys`): its path
  and its fingerprint (OpenSSH's `SHA256:…` of the public key `ssh-keygen
  -y` derives from it, never a `.pub` beside it), never a copy. The file
  must be this user's and readable by its owner alone, as ssh requires,
  and a private key without a passphrase (git-gate can't type one).
- `--write` says the key can push. There's no API to ask, so it's stated.
  git-gate still enforces each grant's read or write; a key that can do less
  than it says fails at the server's push.
- Two forges may name the same file. Removing a key drops only that forge's
  reference to it; the file stays. `forge remove <host>` drops the forge and
  all its references.

At launch:

| Case | What happens |
|---|---|
| no key for the repo, and no default | the bottle doesn't load: "this host holds no key for `<forge>/<path>`", naming the `forge key add` that adds one |
| a `:write` grant whose key is read-only | the bottle doesn't load, naming `forge key add … --write` |
| the server isn't connected | the bottle doesn't load, naming `bot-bottle forge connect ssh://…` from the declaration |
| the key file is missing or unusable (open to others, another user's, a passphrase) | the launch fails, naming the grant and the `forge key add` that replaces it, never the file's path |
| the key's fingerprint changed (rotated in place) | it's used, logged once, and the new fingerprint recorded, so `forge key list` shows it |

The key reaches git-gate as a static key file always did: the repo's
`IdentityFile`, copied into the gateway at registration. An agent's
`add-repo` on an SSH server is granted the same way, with nothing minted.
`create-repo` and `actions.create` are refused there.

Trusting a project bottle records the server's address and pinned host key,
and names each repo's key by its fingerprint, never its path. A key rotated
in place asks again, at the launch that will use it.

#### Forges in the console

The console keeps the account's forge connections
([ADR 0021](../decisions/0021-forge-connections-belong-to-the-account.md)):
what each paired host reports with `forge_status`
([host-controller.md](host-controller.md#forges-for-the-console)), one entry
per forge and login, never a token. A host that holds the token *carries*
the connection. An SSH server's connection reports, per key, the repo (or
the default), read or write and the fingerprint, never a path; its repo list
is the repos some host holds a key for.

- **Forges page.** Lists each connection with its state and the hosts that
  carry it. *Connect a forge* goes through an online host: GitHub's guided
  sign-in and app install, or a Gitea or GitHub Enterprise token, which the
  host keeps.
- **Add a repo.** In a bottle's editor, pick a forge, then a repo from a
  filterable list of the repos the account can reach, asked of a host that
  carries it (`forge_repos`). Write is off for a repo the account can only
  read. Adding a repo writes its forge's declaration into the bottle, so the
  bottle isn't partial. A repo can still be typed by hand.
- **Launch.** A host that hasn't connected a forge the bottle grants on can't
  resolve the bottle. The launch page offers to connect it on that host,
  inline, then resolves again.

A new host connects each forge once: GitHub with a device code (the app's
install is the account's, already done), Gitea with its token. Tokens aren't
copied between hosts.

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
`bot-bottle`, whose client id is built in. `--app` and `--client-id` set
another, and it's kept as the host
setting `github_app`, in the host database.

So each named environment can use its own app.

No private key or client secret of the app is ever on a host or the console.
The device flow and refreshing its tokens take the client id alone
(`contrib/github/user_token.py`).

**The token.** The token goes to the host secrets file under the user's secret
name. The user is recorded on `github.com` with its app's slug
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
  page and waits for the installation to appear.
- `--no-wait` only prints the install link.

The console's host page shows the same two steps. While an `add-repo` card for
an unreachable repo is shown, the host polls GitHub. Once the repo is
reachable, it raises the card again with the grant options.

#### GitHub transports

A GitHub repo is reached over `https` by default, or over `deploy-key`, chosen
per connected user (ADR 0014).

| | `https` (the default) | `deploy-key` |
|---|---|---|
| How | git-gate fetches and pushes `https://github.com/<owner>/<repo>.git` with the forge's token | a key per repo per launch, as for Gitea |
| Minted | nothing | a key per repo per launch |
| Held to granted repos by | git-gate | GitHub itself, per repo |
| Cost | the token reaches every repo the app is installed on | needs admin on the repo, the far broader Administration permission |

The transport is set with `forge connect --transport`, on the connected user.
The app's device flow takes it too.

On a host already connected, `forge connect github --transport T` changes just
the transport. Whoever connects the user chooses. A bottle can't (ADR 0019).

**`https`.** The host copies the current token into the gateway's tmpfs, as
`/dev/shm/git-gate/<bottle_id>/<SECRET>-token`.

- The file is 0600.
- tmpfs is mounted on `/dev/shm` if it isn't one. Provisioning refuses to go
  on without it.
- The copy goes through a 0600 temp file, never a command line.
- On macos-container it's streamed in over `container exec -i`'s stdin.
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

An authorization the renewer can't renew raises a console alert. So does a
Gitea token the forge refuses. The alerts are `forge_alerts` (see host-controller.md's `host_stats`).

**`deploy-key`.** Minting needs the app's Administration: read & write, or a
classic `repo` token from a repo admin.

- A token that can't mint fails the launch at minting, saying so.
- Use it where GitHub itself should hold each run to one repo.
- To use both on one host, connect a second user with `--transport deploy-key`.
  Pick it with an agent's `identity.accounts`.

**The trade-off.** A user token can't be narrowed per repo. Over `https`,
git-gate holds each bottle to its granted repos, with one namespace per bottle
and `git-gate.readOnly` for a grant without `:write`.

The gateway holds the token only while the bottle runs. Install the app on selected repos to keep its
reach small.

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

Trusting a project bottle records this host's connection to each forge its
repos are on (see manifest.md's Trust).

## Agent side

The agent's `~/.gitconfig` sends each repo's upstream URL to the gate
(`control/git_gate_render.py`).

- An `insteadOf` rewrite maps each upstream URL to
  `http://<gateway>:9420/<name>.git`.
- An `http.extraHeader` sends `x-bot-bottle-identity: <token>`, scoped to the
  gate's URL.

The header is written even for a bottle without repos, since every bottle is
given the gate's address. So a repo granted later only adds a rewrite.

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

The launch keeps the value and mode for re-provisioning. A dry run keeps
nothing. They're set on each bare repo as `git-gate.trailer` and
`git-gate.trailers`.

### Re-provisioning state

What the host needs to provision a bottle's git-gate again is rows of the
host database, keyed by the bottled agent's identity (`git_gate/state.py`).

| Table | Holds |
|---|---|
| `git_gate_upstreams` | each repo's upstream, transport, key file, known_hosts file and token name |
| `git_gate_live_grants` | the deploy keys granted while it runs, for teardown to revoke |
| `git_gate_trailers` | the trailer's value and mode |

- Only the host controller reads and writes them. The gateway gets keys,
  repos and config pushed into it.
- They go with the bottled agent's other rows when it's removed.
- The keys themselves stay files in the bottle's git-gate state dir,
  `~/.bot-bottle/state/<identity>/git-gate/`.

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

1. The grant records the key as a row of `git_gate_live_grants`.
2. It mints the key and adds the repo to `git_gate_upstreams`.
3. Re-provisioning the gateway then creates the repo there.
4. `git config --add` in the guest adds its `insteadOf` rewrite.

Keeping the grant writes it into the bottle with the forge's description.

- Only forge grants can be added this way. On an SSH server the grant uses
  the host's key for the repo, as a launch does; nothing is minted.
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
| The forge is an SSH server | refused: it has no API to create a repo with |
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

The access hook runs `git fetch origin --prune` against the upstream before
every `upload-pack`. It uses the repo's key and pinned known_hosts, or HTTPS
with the token for a GitHub repo ([Transports](#github-transports)).

If the upstream is unreachable, the fetch fails instead of serving stale data.

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

A bottle whose repos are written in long form, with their own `url`, `key`
and `host_key`, no longer loads, whatever the key. Its error lists each such
repo and shows the replacement (`manifest/legacy_git_gate.py`). The library's
parse check (a hand edit on a host, a save in the console) refuses it the
same way.

1. Connect the forge once with `bot-bottle forge connect <url>`, unless the
   error says it's connected already.
2. Run `bot-bottle bottles migrate <bottle>` (`manifest/migrate.py`). It shows
   the diff and asks first, or doesn't with `--yes`.

A repo with a `static` key file becomes a grant on an SSH server
([SSH servers](#ssh-servers)): `migrate` declares the server as a forge of kind
`ssh`, from the repo's `url` (its address) and `host_key`, and grants the repo
with `:write`, as the key could push. The grant names the repo's path from
the `url` verbatim (`ssh://git@h:22/srv/git/app.git` becomes
`h/srv/git/app.git:write`), whatever its depth, so a static repo needs no
hand edit. Where the long form's name isn't the one the grant gives (its
path's last segment without `.git`), the mapping form keeps it. It does this
whether or not the server is connected here. Until `bot-bottle forge connect
ssh://…` and `bot-bottle forge key add <host> <path> <key file> --write`,
which the error and `migrate` both print, the bottle doesn't load on this
host. The connect
command passes the repo's pinned `host_key` as `--host-key`, so the pin
carries over. A static repo on a host connected as Gitea or GitHub becomes
a grant on that forge instead, so its path must be `<owner>/<repo>`; only a
path no grant can name (a `~`, say) is left to rewrite by hand.

A forge with `credentials:` or a `token` also fails to load. Connect it again
with `forge connect`, then remove those lines (ADR 0019).
