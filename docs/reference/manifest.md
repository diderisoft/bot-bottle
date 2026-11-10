# Manifest

The files that say what an agent is and what its bottle lets it do. Code is in
`manifest/` (see the [code map](architecture.md#code-map)). How a launch uses
the result is in [architecture.md](architecture.md#launch).

## Files

Bottles and agents are Markdown files with YAML frontmatter.

| Kind | Yours | A repo's | Body |
|---|---|---|---|
| Bottle | `~/.bot-bottle/bottles/<name>.md` | `<repo>/.bot-bottle/bottles/<name>.md`, named `./<name>` | documentation |
| Agent | `~/.bot-bottle/agents/<name>.md` | `<repo>/.bot-bottle/agents/<name>.md` | the agent's system prompt |

| File | Holds |
|---|---|
| Bottle | what the agent may reach and with which credentials: its env, git identity, git-gate repos and egress routes |
| Agent | who it is: its harness (`agent_provider`), its skills and its prompt |

An agent doesn't name a bottle. You pick bottles at launch: `--bottle`, the
console's launch page, or a suggested one in the guided `bot-bottle start`
([bottles-suggest.md](bottles-suggest.md)).

A launch with none runs in the built-in `basic`. So an agent file works on any
host, whatever its bottles are called
([ADR 0011](../decisions/0011-agents-dont-name-bottles.md)).

Unknown keys fail at load, naming the key and the accepted ones. More examples
are in [`examples/`](../../examples/).

## Bottles

A bottle grants repos, env and egress routes. `~/.bot-bottle/bottles/app-dev.md`:

````markdown
---
extends: basic           # start from the built-in package-registry routes

env:
  NODE_ENV: development

git-gate:
  user:
    name: app-bot
    email: app-bot@example.com
  repos: [github.com/acme/app:write]   # a grant on a connected forge

egress:
  routes:
    - host: api.example.com
      inspect:
        auth:
          scheme: Bearer       # Bearer | token
          token_ref: EXAMPLE_API_TOKEN
        matches:               # optional: restrict to specific paths/methods/headers
          - paths:
              - type: prefix
                value: /v1/
            methods: [GET, POST]
        outbound_detectors: [token_patterns, known_secrets]
        inbound_detectors: [naive_injection_detection]   # opt in to response scanning
---

The `app-dev` bottle: push to `acme/app` on GitHub, a token over HTTPS for the
API. The agent's provider adds its own model route.
````

Where `env` entries and `token_ref` find their values: see [secrets.md](secrets.md).

The grant `github.com/acme/app:write` is read-only without `:write`. It needs
this host to have connected the forge ([Forges](#forges)).

A repo on a forge bot-bottle doesn't support yet takes the long form instead,
with its own `url`, a `static` key file and the pinned `host_key`:

```yaml
git-gate:
  repos:
    app:
      url: ssh://git@git.example.com/acme/app.git
      key:
        provider: static
        path: /path/to/app_deploy_key
      host_key: ssh-ed25519 AAAA...
```

This one pushes to `app` over SSH.

The whole `git-gate` block, including forges and one-line repo grants, is in
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
| `git-gate.repos` | per repo name, field by field. `repos: {}` clears them |
| `git-gate.trailers`, `git-gate.protect_default_branch` | the later bottle's, when it sets one |
| `git-gate.actions.create` | joined, each pattern once |
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
  forward_host_credentials: true
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
| `agent_provider.dockerfile` | the agent's own Dockerfile, a custom image |
| `agent_provider.overlay` | layers onto the built-in image ([images](#images)) |
| `forward_host_credentials`, `auth_token` | the agent's login ([login](#login)) |
| `skills` | names of skill directories copied from the host's `~/.claude/skills/<name>/` into the agent |
| `forge_users` | `{<forge host>: <user>}`: which user connected on a forge the agent pushes as |

A forge `forge_users` doesn't name uses its default user
([git-gate.md](git-gate.md#forges-and-one-line-grants)). Grants name no user,
so they stay portable.

### Images

Each harness has an image built from `bot_bottle/contrib/<harness>/Dockerfile`.
An agent can supply its own Dockerfile with `agent_provider.dockerfile`, or
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
or complex image, use `agent_provider.dockerfile` (a custom image) instead.

### Login

`forward_host_credentials: true` uses the host's Claude Code or Codex login. A
Claude agent can instead name, in `auth_token`, a host variable holding a
long-lived OAuth token (`claude setup-token`).

Either way the gateway injects it and the agent sees a placeholder
([install.md](install.md#signing-in-an-agent)).

The provider adds its own model route. A bottle route for that host fails the
launch.

## Trust

Your own bottles and agents run as written. A repo's run only once this host
trusts their exact contents (`bot-bottle trust <name>`), and any change asks
again.

Trust records live in `~/.bot-bottle/trusted-bottles.json`. What the trust hash
covers is in [architecture.md](architecture.md#state).

### Repo bottles

A repo bottle always needs trust.

- Repo bottles are always named `./<name>`, so a repo bottle never shares a
  name with one of yours. `bot-bottle <agent>` uses `./main` when the repo has
  one.
- The trust prompt lists every host and secret the bottle touches, including
  what it inherits through `extends`. So a cloned repo can't send a credential
  to another host without you seeing it first.
- Trust also records this host's connection to each forge the bottle's repos
  are on ([below](#what-forge-trust-records)).
- When one changes, the bottle asks to be trusted again, showing what changed
  (ADR 0019). Your own bottles don't.

#### What forge trust records

For each forge, trust records its `kind`, `url`, `ssh` and host key. It also
records the user its grants push as, with that user's transport.

That user is the forge's default user, or the one the agent's `forge_users`
picks. So an agent can't switch to another of your accounts unseen.

### Repo agents

A repo agent needs trust when its `agent_provider` grants something:

- your login
- a model host with a secret
- an image build

It also needs trust when it runs on one of your bottles that grants more than
`basic`. A repo agent with the same name as one of yours replaces it, and the
trust prompt says so.

## Forges

`bot-bottle forge connect <url>` lets bottles push to a Gitea or GitHub forge.

1. It checks a token.
2. It pins the forge's host key.
3. It records the connection in `~/.bot-bottle/forges.md`, under the user the
   token belongs to.

Any bottle then grants repos in a line, with nothing to extend:

```yaml
git-gate: {repos: [gitea.example.com/acme/app:write]}
```

- Each grant is read-only unless `:write`.
- A bottle may describe the forge (`git-gate.forges`), so it says what to
  connect on another machine.
- A bottle never carries a token.

Details are in [git-gate.md](git-gate.md#forges-and-one-line-grants) and
[ADR 0019](../decisions/0019-forges-are-described-in-bottles-connected-on-hosts.md).

A bottle written before forges no longer loads. That is one with each repo's
own `url`, `key: {provider: gitea, ...}` and `host_key`.

Its error says how to convert it. `bot-bottle bottles migrate <name>` does it
as a new version. See
[Migrating from the old format](git-gate.md#migrating-from-the-old-format).

## Suggesting a bottle

`bot-bottle bottles suggest "<task>"` proposes the bottle a task needs without
writing anything. The full reference is [bottles-suggest.md](bottles-suggest.md).

- It asks the `bottle-architect` agent: your own harness, run with no tools.
- It falls back to the rules path. `--rules` uses the rules path only.
- `--write NAME` saves the proposal as a new host bottle.

## Versions

Every change to a bottle or agent under `~/.bot-bottle/` is a version, hand
edits included.

| Command | Does |
|---|---|
| `bot-bottle bottles history <name>` | lists them. Add a version to see what it changed |
| `bot-bottle bottles revert <name> <version>` | restores one as a new version, taking effect at the next launch |
| `bot-bottle bottles migrate <name>` | rewrites a bottle's old-format git-gate repos as forge grants, as a new version ([git-gate.md](git-gate.md#migrating-from-the-old-format)) |

A repo's bottles are versioned by the repo itself. Where versions are kept is
in [architecture.md](architecture.md#state).

## Egress routes

`egress.routes` is the bottle's allowlist. Every host not on it is refused,
DNS-over-HTTPS resolvers included.

A route has `host`, an optional `role`, and an optional `inspect` block holding
everything else. How the proxy applies a route is in
[gateway.md](gateway.md#egress-proxy).

| Field | Required | Description |
|---|---|---|
| `host` | yes | Hostname to allowlist. One entry per host in a bottle. A later bottle's entry for the host replaces an inherited one ([composing](#bottles)). |
| `blocked` | no | `true` takes an inherited `host` away. The entry then has only `host` and `blocked`. |
| `role` | no | Reserved for future use. The key is recognised but any value is currently rejected at load. Provider auth routes (e.g. Claude's `api.anthropic.com`) are injected automatically from the agent's `agent_provider`, not via `role`. A bottle route for such a host fails the launch. |
| `inspect` | no | `false` passes the TLS tunnel through untouched: no DLP, auth injection or path matching. Otherwise a mapping of the fields below. |
| `inspect.auth.scheme` | when `auth` present | `Bearer` or `token`. Injected by the proxy. The agent never sees the value. |
| `inspect.auth.token_ref` | when `auth` present | Env-var name holding the secret on the host. |
| `inspect.matches` | no | Array of `{paths, methods, headers}` filters. A request must match at least one entry (if any are given) to be forwarded. An inspected route with no `matches` and no gateway-injected auth (`token_env`/`preserve_auth`) is read-only: only `GET` and `HEAD` pass, so list the methods (e.g. `methods: [GET, POST]`) to allow writes. |
| `inspect.matches[].paths` | no | Array of `{type, value}`. `type` is `prefix` (default), `exact`, or `regex`. |
| `inspect.matches[].methods` | no | Array of HTTP method strings, e.g. `[GET, POST]`. |
| `inspect.matches[].headers` | no | Array of `{name, value, type}`. `type` is `exact` (default) or `regex`. |
| `inspect.outbound_detectors` | no | `false` turns off the content detectors. A list runs only those named (`token_patterns`, `known_secrets`, `entropy`). Omit for the defaults, `token_patterns` and `known_secrets` (`entropy` runs only when listed). The canary and CRLF checks run on every inspected route regardless. |
| `inspect.inbound_detectors` | no | Off unless listed: name the detectors to run on responses (`naive_injection_detection`). Omit or `false` for none. |
| `inspect.outbound_on_match` | no | What to do when an outbound token is detected: `supervise` (default for manifest routes, held for operator approval), `redact` (scrub the value and forward), or `block` (hard 403). Agent-provider routes (e.g. `api.anthropic.com`) default to `redact`. |
| `inspect.preserve_auth` | no | `true` forwards the client's own `Authorization` header instead of stripping it (registries whose token dance fetches per-scope tokens). |
| `inspect.git.fetch` | no | `true` permits smart HTTP clone/fetch (`git-upload-pack`) for this host. Push (`git-receive-pack`) remains blocked. Pushes go through [git-gate](git-gate.md). |

### Egress log

`egress.log` sets how much the proxy writes about the bottle's traffic. It's
separate from the audit journal and the [firehose](gateway.md#request-firehose).

| Value | What's logged |
|---|---|
| `0` (default) | Nothing beyond the audit journal. |
| `1` | One summary line per blocked or flagged request, in the gateway's log. |
| `2` | Also every inspected request and response, headers and body (secrets redacted, each body cut at 64 KiB), to an owner-only file in the gateway: `/var/log/bot-bottle-egress/full.jsonl`, rotated at 16 MiB with three older files kept. A launch at `log: 2` warns about this. It holds whole conversations, so use it only for debugging. |

### When outbound DLP matches

The route's `inspect.outbound_on_match` decides what happens when an outbound
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
