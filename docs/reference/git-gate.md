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
  `contrib/gitea/deploy_key_provisioner.py`).

Without a `host_key`, an interactive launch fetches one with `ssh-keyscan`,
asks the operator to confirm it, and can save it to the bottle file
(`git_gate/host_key.py`). A headless launch refuses to start.

## Agent side

`control/git_gate_render.py` writes the agent's `~/.gitconfig`:

- an `insteadOf` rewrite from each upstream URL to
  `http://<gateway>:9420/<name>.git`;
- an `http.extraHeader` sending `x-bot-bottle-identity: <token>`, scoped to
  the gate's URL.

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
the running gateway and inits its bare repos at registration. On a gateway
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
