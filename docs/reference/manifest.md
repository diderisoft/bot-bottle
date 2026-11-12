# Manifests

The files that say what an agent is and what its bottle lets it do. Code is in
`manifest/` (see the [code map](architecture.md#code-map)). How a launch uses
the result is in [architecture.md](architecture.md#launch).

## Files

Bottles and agents are Markdown files with YAML frontmatter.

| Kind | Yours | A project's | Body |
|---|---|---|---|
| Bottle | `~/.bot-bottle/bottles/<name>.md` | `<repo>/.bot-bottle/bottles/<name>.md`, named `./<name>` | documentation |
| Agent | `~/.bot-bottle/agents/<name>.md` | `<repo>/.bot-bottle/agents/<name>.md` | the agent's system prompt |

| File | Holds |
|---|---|
| Bottle | what the agent may reach and with which credentials: its env, git identity, `git_gate` repos and egress routes |
| Agent | who it is: its harness (`agent_provider`), its skills and its prompt |

An agent doesn't name a bottle. You pick bottles at launch: `--bottle`, the
console's launch page, or a suggested one in the guided `bot-bottle start`
([architect.md](architect.md)).

A launch with none runs in the built-in `basic`. So an agent file works on any
host, whatever its bottles are called
([ADR 0011](../decisions/0011-agents-dont-name-bottles.md)).

A project's bottles and agents run only once this host trusts them
([projects.md](projects.md)).

Unknown keys fail at load, naming the key and the accepted ones. More examples
are in [`examples/`](../../examples/).

## Bottles

A bottle grants repos, env and egress routes. `~/.bot-bottle/bottles/app-dev.md`:

````markdown
---
extends: basic           # start from the built-in package-registry routes

env:
  NODE_ENV: development

git_gate:
  user:
    name: app-bot
    email: app-bot@example.com
  repos: [github.com/acme/app:write]   # a grant on a connected forge

egress:
  routes:
    - host: api.example.com
      auth: {type: bearer, value: secret:EXAMPLE_API_TOKEN}   # bearer | token | header
      allow:                   # optional: restrict to specific paths/methods/headers
        - paths: [/v1/]
          methods: [GET, POST]
      dlp:
        outbound: [token_patterns, known_secrets]
        inbound: [naive_injection_detection]   # opt in to response scanning
---

The `app-dev` bottle: push to `acme/app` on GitHub, a token over HTTPS for the
API. The agent's provider adds its own model route.
````

Where `env` entries and `auth.value` find their values: see [secrets.md](secrets.md).

The grant `github.com/acme/app:write` is read-only without `:write`. It needs
this host to have connected the forge ([Forges](#forges)).

A repo on a forge bot-bottle doesn't support yet takes the long form instead,
with its own `url`, a `static` key file and the pinned `host_key`:

```yaml
git_gate:
  repos:
    app:
      url: ssh://git@git.example.com/acme/app.git
      key:
        provider: static
        path: /path/to/app_deploy_key
      host_key: ssh-ed25519 AAAA...
```

This one pushes to `app` over SSH.

The whole `git_gate` block, including forges and one-line repo grants, is in
[git-gate.md](git-gate.md#manifest).

| Field | Reference |
|---|---|
| `preserve_container` | [backends.md](backends.md#keeping-the-guest) |
| `nested_containers` | [backends.md](backends.md#containers-inside-a-bottle) |
| `resources` (memory and vCPUs) | [backends.md](backends.md#memory-and-vcpus) |

### Composing bottles

`extends:` names one bottle or a list. Keep shared policy in a base bottle and
overlay task bottles on top.

The built-in `basic` bottle (`manifest/builtin/basic.md`) allows downloads from
the common package registries. The agent picks its own harness, so one bottle
serves Claude, Codex and pi agents alike.

What `extends` resolves to is the sealed bottle
([glossary](glossary.md#sealed-bottle)).

The later bottle wins:

- a child over its parents
- a later parent over an earlier one
- a later `--bottle` over an earlier one

| Field | How it combines |
|---|---|
| `env` | per variable |
| `resources` | per key (`memory`, `cpus`) |
| `git_gate.repos` | per repo name, field by field. `repos: {}` clears them |
| `git_gate.trailers`, `git_gate.protect_default_branch` | the later bottle's, when it sets one |
| `git_gate.actions.create` | joined, each pattern once |
| `egress.routes` | per host, case-insensitive (below) |

A later route for a host replaces the earlier one in its place. New hosts
follow in order.

To take an inherited host away, list it as
`- {host: api.example.com, blocked: true}`. Egress is default-deny, so a
blocked host is unreachable.

- `blocked` takes only `host`, and must be `true`.
- Blocking a host nothing routes is allowed and does nothing.
- A later bottle that routes the host unblocks it.
- One bottle can't name a host twice, routed or blocked.

See [ADR 0015](../decisions/0015-egress-later-wins-per-host.md).

## Agents

An agent file names a harness, its skills and its prompt.
`~/.bot-bottle/agents/app-helper.md`:

````markdown
---
agent_provider:          # the harness and its login; default is `claude`
  harness: claude
  login: host
skills:
  - code-review
---

You help maintain the acme app.
````

Run it in the `app-dev` bottle with
`bot-bottle start app-helper --bottle app-dev`. Bare `bot-bottle app-helper`
uses the repo's `./main` bottle when it has one, else `basic`.

An agent file that still has `bottle:` fails to load. The error says to remove
the line and pick the bottle at launch.

| Field | Does |
|---|---|
| `agent_provider.harness` | `claude`, `codex` or `pi` ([images](#images)) |
| `agent_provider.dockerfile_path` | the agent's own Dockerfile, a custom image |
| `agent_provider.overlay` | layers onto the built-in image ([images](#images)) |
| `agent_provider.login` | the agent's login ([login](#login)) |
| `skills` | names of skill directories copied from the host's `~/.claude/skills/<name>/` into the agent |
| `identity.git` | `{name, email}`: the commit author, per part over the bottle's `git_gate.user`. A part neither sets comes from the agent's name ([git-gate.md](git-gate.md#manifest)). |
| `identity.accounts` | `{<forge host>: <user>}`: which user connected on a forge the agent pushes as |

Claude Code's own fields (`name`, `description`, `model`, `color`, `memory`)
are ignored; `model` warns.

A forge `identity.accounts` doesn't name uses its default user
([git-gate.md](git-gate.md#forges-and-one-line-grants)). Grants name no user,
so they stay portable.

### Images

Each harness has an image built from `bot_bottle/contrib/<harness>/Dockerfile`.
An agent can supply its own Dockerfile with `agent_provider.dockerfile_path`, or
layer onto the built-in image with `agent_provider.overlay`.

```yaml
agent_provider:
  overlay:
    dockerfile: |
      FROM ${BASE}
      RUN curl -fsSL https://fly.io/install.sh | FLYCTL_INSTALL=/usr/local sh
```

The agent file is the single source of truth. The overlay fields are:

- `apt`: Debian package names
- `harness_version`
- `files`: paths relative to the launch cwd
- `dockerfile`: inline Dockerfile content, written as the YAML block scalar `|`

All overlays must be single-stage and build `FROM ${BASE}`. bot-bottle pins
`BASE` to the verified, digest-pinned image.

`overlay.dockerfile` is exclusive with the other overlay fields. For a shared
or complex image, use `agent_provider.dockerfile_path` (a custom image) instead.

### Login

`agent_provider.login` is one of:

| Write | Uses | Harnesses |
|---|---|---|
| `login: host` | the host's Claude Code or Codex login | `claude`, `codex` |
| `login: {oauth_token: secret:NAME}` | a long-lived OAuth token (`claude setup-token`) | `claude` |
| `login: {api_key: secret:NAME}` | an API key | `claude`, `pi` |

Either way the gateway injects it and the agent sees a placeholder
([install.md](install.md#signing-in-an-agent)).

The provider adds its own model route. A bottle route for that host fails the
launch.

## Forges

`bot-bottle forge connect <url>` lets bottles push to a Gitea or GitHub forge.

1. It checks a token.
2. It pins the forge's host key.
3. It records the connection in the host database, under the user the token
   belongs to.

`bot-bottle forge list` shows what this host connected:

```
$ bot-bottle forge list
gitea.example.com            gitea   alice*, codex
```

`*` marks the default user. `forge show <host>` adds the SSH address, pinned
host key and token names. `forge remove <host> [--user U]` disconnects.

Any bottle then grants repos in a line, with nothing to extend:

```yaml
git_gate:
  forges:
    gitea.example.com: { kind: gitea }
  repos: [gitea.example.com/acme/app:write]
```

- Each grant is read-only unless `:write`.
- The grant's forge must be declared (`git_gate.forges`) somewhere in the
  bottle's chain, so the bottle says what to connect on another machine.
  Without it the bottle is *partial*: it loads and can be extended, but
  can't be launched alone. `bot-bottle bottles migrate <name>` adds the
  declaration from this host's connection.
- A bottle never carries a token.

Details are in [git-gate.md](git-gate.md#forges-and-one-line-grants) and
[ADR 0019](../decisions/0019-forges-are-described-in-bottles-connected-on-hosts.md).

A bottle written before forges no longer loads. That is one with each repo's
own `url`, `key: {provider: gitea, ...}` and `host_key`.

Its error says how to convert it. `bot-bottle bottles migrate <name>` does it
as a new version. See
[Migrating from the old format](git-gate.md#migrating-from-the-old-format).

## Versions

Every change to a bottle or agent under `~/.bot-bottle/` is a version, hand
edits included.

| Command | Does |
|---|---|
| `bot-bottle bottles history <name>` | lists them. Add a version to see what it changed |
| `bot-bottle bottles revert <name> <version>` | restores one as a new version, taking effect at the next launch |
| `bot-bottle bottles migrate <name>` | rewrites a bottle's old forms as a new version: `git-gate` as `git_gate`, old-format repos as forge grants, and the old route, auth and `log` forms ([git-gate.md](git-gate.md#migrating-from-the-old-format)) |

A repo's bottles are versioned by the repo itself. Where versions are kept is
in [architecture.md](architecture.md#state).

## Egress routes

`egress.routes` is the bottle's allowlist. Every host not on it is refused,
DNS-over-HTTPS resolvers included.

A route has `host` and the optional keys below. Unknown keys are errors. How
the proxy applies a route is in [gateway.md](gateway.md#egress-proxy).

```yaml
egress:
  routes:
    - host: registry.npmjs.org # GET and HEAD only (the default)
    - host: api.example.com
      methods: [GET, POST]     # or ["*"] for any method
      allow:                   # optional: narrow further
        - paths: [/v1/]        # prefix; or {exact: /x} or {regex: ...}
          methods: [POST]
      auth: {type: bearer, value: secret:EXAMPLE_API_TOKEN}
    - host: cdn.example.com
      passthrough: true        # no inspection at all; takes no other keys
```

| Field | Required | Description |
|---|---|---|
| `host` | yes | Hostname to allowlist. One entry per host in a bottle. A later bottle's entry for the host replaces an inherited one ([composing](#bottles)). |
| `blocked` | no | `true` takes an inherited `host` away. The entry then has only `host` and `blocked`. |
| `passthrough` | no | `true` passes the TLS tunnel through untouched: no DLP, canary, credential stripping, method rules or push block. An unscanned channel. The route then takes no other keys. |
| `methods` | no | The HTTP methods the route allows, e.g. `[GET, POST]`. Without it, every inspected route allows only `GET` and `HEAD`, with or without `auth`, unless an `allow` entry lists its own. `["*"]` allows any method and stands alone. |
| `allow` | no | Array of `{paths, methods, headers}` filters. A request must match at least one entry (if any are given) to be forwarded. |
| `allow[].paths` | no | Array of paths. A bare string is a path prefix; `{exact: /x}` and `{regex: ...}` match exactly or by regex. |
| `allow[].methods` | no | Array of HTTP method strings. Defaults to the route's `methods`. |
| `allow[].headers` | no | Array of `{name, value, type}`. `type` is `exact` (default) or `regex`. |
| `auth` | no | `{type, value}`, injected by the proxy. The agent never sees the value. `type: bearer` sends `Authorization: Bearer <value>`, `type: token` sends `Authorization: token <value>`, and `type: header` with `name: X-Api-Key` sends `X-Api-Key: <value>` (a valid, unreserved header name). `value` is `secret:NAME` or `secret:${VAR}` ([secrets.md](secrets.md)). Provider auth routes (e.g. Claude's `api.anthropic.com`) are injected automatically from the agent's `agent_provider`. A bottle route for such a host fails the launch. |
| `dlp` | no | `false` turns off the content detectors. Otherwise `{outbound, inbound, on_match}`. A listed side runs exactly the detectors named; an omitted side keeps the defaults. `outbound` (`token_patterns`, `known_secrets`, `entropy`) defaults to `token_patterns` and `known_secrets` (`entropy` runs only when listed). `inbound` (`naive_injection_detection`) runs none unless listed. The canary and CRLF checks run on every inspected route regardless. |
| `dlp.on_match` | no | What to do when an outbound token is detected: `supervise` (default for manifest routes, held for operator approval), `redact` (scrub the value and forward), or `block` (hard 403). Agent-provider routes (e.g. `api.anthropic.com`) default to `redact`. |
| `preserve_auth` | no | `true` forwards the client's own `Authorization` header instead of stripping it (registries whose token dance fetches per-scope tokens). |
| `git.fetch` | no | `true` permits smart HTTP clone/fetch (`git-upload-pack`) for this host. Push (`git-receive-pack`) remains blocked. Pushes go through [git-gate](git-gate.md). |

### Egress log

`egress.log` sets how much the proxy writes about the bottle's traffic. It's
separate from the audit journal and the [firehose](gateway.md#request-firehose).

| Value | What's logged |
|---|---|
| `off` (default) | Nothing beyond the audit journal. |
| `blocked` | One summary line per blocked or flagged request, in the gateway's log. |
| `full` | Also every inspected request and response, headers and body (secrets redacted, each body cut at 64 KiB), to an owner-only file in the gateway: `/var/log/bot-bottle-egress/full.jsonl`, rotated at 16 MiB with three older files kept. A launch at `log: full` warns about this. It holds whole conversations, so use it only for debugging. |

### When outbound DLP matches

The route's `dlp.on_match` decides what happens when an outbound
detector matches a token.

| Policy | What happens |
|---|---|
| `supervise` (default) | the request is held for the operator to approve ([below](#supervise-policy)) |
| `redact` | the matched value is scrubbed from the body, headers and path, and the request is forwarded |
| `block` | a hard `403` |

`redact` fails closed if a match lands somewhere unredactable, like the
hostname.

Structural CRLF-injection blocks, canary hits and not-in-allowlist host
blocks are always hard `403`s regardless of policy
([gateway.md](gateway.md#outbound-dlp)).

#### Supervise policy

The proxy queues an `egress-token-allow` proposal for the operator's
`bot-bottle supervise` TUI. It holds the request open until it is answered.

If `EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS` (default 300) elapses first, the
request fails closed.

The operator never sees the raw token, only the host, method, path and a
redacted snippet.

Approving adds the value to an in-memory safelist for that bottle and that
host, for the life of the egress proxy. The same value sent to another host is
flagged again.
