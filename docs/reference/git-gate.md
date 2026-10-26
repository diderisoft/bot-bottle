# git-gate

git-gate is the gateway's git server. Agents push and fetch through it and
never hold an upstream credential. Pushes are scanned and forwarded upstream
over SSH. Fetches refresh from upstream first, so the agent sees current
state.

## Manifest

```yaml
git-gate:
  user:                       # optional: the agent's git identity
    name: implementer-bot
    email: bot@example.com
  repos:
    bot-bottle:               # local name: the gate's bare repo
      url: ssh://git@gitea.example.com:30009/org/bot-bottle.git
      key:
        provider: gitea       # or: static (with path: /host/key)
        forge_token_env: BOT_BOTTLE_GITEA_TOKEN
      host_key: ssh-ed25519 AAAA...   # pins the upstream host key
```

Only `ssh://user@host[:port]/path` upstreams are supported (`manifest/git.py`).
Key providers:

- `static`: a key file already on the host.
- `gitea`: a fresh ed25519 deploy key is added through the forge API at
  launch and revoked at teardown (`git_gate/provision.py`,
  `contrib/gitea/deploy_key_provisioner.py`). `read_only: true` mints one
  that can't push.

Without a `host_key`, an interactive launch fetches one with `ssh-keyscan`,
asks the operator to confirm it, and can save it to the bottle file
(`git_gate/host_key.py`). A headless launch refuses to start.

### Forges and one-line grants

A bottle can declare a forge once and grant repos on it in a line
(`manifest/forges.py`, [PRD 0103](../prds/0103-forge-connect-and-bottle-onboarding.md)).
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
(`cli/commands/forge.py`, `forge_connect.py`). It checks the forge answers as
Gitea, takes a token (pasted without echo, or `--token-stdin`) and asks the
forge whose it is and which of its repos it's admin on, warning when none
(deploy keys need admin). It pins the SSH host key it finds for the forge's
repos without asking, stores the token in the host secrets file as
`<NAME>_TOKEN`, and writes the credential, the forge entry and a read-only
`GET` route for the forge's packages and release downloads into `<bottle>`
(default `gitea`; `./<name>` writes a project bottle and trusts it), as a
new version. `<name>` is the forge key: the host by default, or
`<account>@<host>` when the bottle already has the host for another account.
Keys that aren't bare identifiers are written quoted
(`"codex@gitea.example.com":`).

While `extends` is resolved, each grant becomes the long form above: a
`gitea` key minted per launch with the forge's token, the forge's host key,
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

## Agent side

`control/git_gate_render.py` writes the agent's `~/.gitconfig`:

- an `insteadOf` rewrite from each upstream URL to
  `http://<gateway>:9420/<name>.git`;
- an `http.extraHeader` sending `x-bot-bottle-identity: <token>`, scoped to
  the gate's URL. It's written even for a bottle without repos, so a repo
  granted later only adds a rewrite. A bottle launched before that gets it
  when it's first granted a repo, from its agent's proxy URL
  (`http://bottle:<token>@<gateway>:<port>`), whose host is the gate's on
  every backend.

The agent's `NO_PROXY` covers the gateway, so git traffic to it skips the
egress proxy. Smart-HTTP push to a forge through the egress proxy is blocked,
so the gate is the only way to push.

## Gateway side

`gateway/git_gate/http_backend.py` serves every bottle from one process:

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
(`git_gate/live_grant.py`, `control/hostctl/repo_grants.py`,
[PRD 0105](../prds/0105-forges-and-repos-from-the-phone.md)). The host finds
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
2. Runs gitleaks over the commits the push introduces: those reachable from
   the new tip and not from any ref the gate already has
   ([PRD 0028](../prds/0028-git-gate-new-branch-scan-scope.md)). The rules
   come from `git_gate_render_gitleaks_config`. A finding refuses the push.
3. Re-scans ignoring `# gitleaks:allow` comments. A finding hidden only by an
   inline suppression becomes a `gitleaks-allow` supervise proposal. The push
   waits for the operator and is refused on rejection or timeout.
4. Runs `gateway/git_gate/held_secret_scan.py`, which looks for the pushing
   bottle's own secrets in every ref name, commit message, identity, path and
   added line the push introduces. Those secrets are its `?` env values, its
   egress tokens and its canary. It matches by value, so it catches secrets
   gitleaks has no rule for.
5. Forwards each accepted ref to `origin` over SSH, checking the pinned host
   key.

The hook scans every ref before forwarding any, so a hit on one ref can't
leave earlier refs half-pushed. Each decision is an audit event from the
`git-gate` producer. Under `audit.delivery: required`, a ref whose record
can't be written is refused.
