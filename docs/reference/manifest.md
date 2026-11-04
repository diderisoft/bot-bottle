# Manifest

The files that say what an agent is and what its bottle lets it do. Code is in
`manifest/` (see the [code map](architecture.md#code-map)); how a launch uses
the result is in [architecture.md](architecture.md#launch).

## Files

Bottles and agents are Markdown files with YAML frontmatter.

| Kind | Yours | A repo's | Body |
|---|---|---|---|
| Bottle | `~/.bot-bottle/bottles/<name>.md` | `<repo>/.bot-bottle/bottles/<name>.md`, named `./<name>` | documentation |
| Agent | `~/.bot-bottle/agents/<name>.md` | `<repo>/.bot-bottle/agents/<name>.md` | the agent's system prompt |

A bottle holds what the agent may reach and with which credentials: its env,
git identity, git-gate repos and egress routes. An agent holds who it is: its
harness (`agent_provider`), its skills and its prompt. An agent doesn't name
a bottle: you pick bottles at launch (`--bottle`, or the console's launch
page), and a launch with none runs in the built-in `basic`. So an agent file
works on any host, whatever its bottles are called
([ADR 0011](../decisions/0011-agents-dont-name-bottles.md)). Unknown keys fail
at load, naming the key and the accepted ones.

More examples are in [`examples/`](../../examples/).

## Bottles

`~/.bot-bottle/bottles/app-dev.md`:

````markdown
---
extends: basic           # start from the built-in package-registry routes

env:
  NODE_ENV: development

git-gate:
  user:
    name: app-bot
    email: app-bot@example.com
  repos:
    app:
      url: ssh://git@git.example.com/acme/app.git
      key:
        provider: static
        path: /path/to/app_deploy_key
      host_key: ssh-ed25519 AAAA...

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

The `app-dev` bottle: push to `app` over SSH, a token over HTTPS for the
API. The agent's provider adds its own model route.
````

**Composing bottles.** `extends:` names one bottle or a list. Keep shared
policy in a base bottle and overlay task bottles on top. The built-in `basic`
bottle (`manifest/builtin/basic.md`) allows downloads from the common package
registries. The agent picks its own harness, so one bottle serves Claude,
Codex and pi agents alike. What `extends` resolves to is the sealed bottle
([glossary](glossary.md#sealed-bottle)).

How the fields combine, later bottle winning (a child over its parents, a
later parent over an earlier one, a later `--bottle` over an earlier one):

- `env`: per variable.
- `git-gate.repos`: per repo name, field by field; `repos: {}` clears them.
- `git-gate.trailers` and `git-gate.protect_default_branch`: the later bottle's,
  when it sets one.
- `egress.routes`: per host, case-insensitive. A later route for a host
  replaces the earlier one in its place; new hosts follow in order. To take
  an inherited host away, list it as `- {host: api.example.com, blocked: true}`:
  egress is default-deny, so a blocked host is unreachable. `blocked` takes
  only `host`, and must be `true`. Blocking a host nothing routes is allowed
  and does nothing, and a later bottle that routes the host unblocks it. One
  bottle can't name a host twice, routed or blocked. See
  [ADR 0015](../decisions/0015-egress-later-wins-per-host.md).

The `git-gate` block, including forges and one-line repo grants, is in
[git-gate.md](git-gate.md#manifest). `preserve_container` is in
[backends.md](backends.md#keeping-the-guest), and `nested_containers` in
[backends.md](backends.md#containers-inside-a-bottle).

## Agents

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

Run it in the `app-dev` bottle with `bot-bottle start app-helper --bottle
app-dev`. Bare `bot-bottle app-helper` uses the repo's `./main` bottle when it
has one, else `basic`. An agent file that still has `bottle:` fails to load,
saying to remove the line and pick the bottle at launch.

- **`agent_provider.harness`**: `claude`, `codex` or `pi`. Each has an image
  built from `bot_bottle/contrib/<harness>/Dockerfile`; an agent can supply
  its own Dockerfile (`agent_provider.dockerfile`) or layer onto the built-in
  image (`agent_provider.overlay`). The overlay fields are `apt` (Debian
  package names), `harness_version`, `files` (paths relative to the launch
  cwd), and `dockerfile` (inline Dockerfile content — use the YAML block
  scalar `|`). The agent file is the single source of truth:

  ```yaml
  agent_provider:
    overlay:
      dockerfile: |
        FROM ${BASE}
        RUN curl -fsSL https://fly.io/install.sh | FLYCTL_INSTALL=/usr/local sh
  ```

  All overlays must be single-stage and build `FROM ${BASE}`; bot-bottle pins
  `BASE` to the verified, digest-pinned image. `overlay.dockerfile` is
  exclusive with the other overlay fields. For a shared or complex image,
  use `agent_provider.dockerfile` (a custom image) instead.
- **Login.** `forward_host_credentials: true` uses the host's Claude Code or
  Codex login. A Claude agent can instead name a host variable holding a
  long-lived OAuth token (`claude setup-token`) in `auth_token`. Either way
  the gateway injects it and the agent sees a placeholder
  ([install.md](install.md#signing-in-an-agent)). The provider adds its own
  model route; a bottle route for that host fails the launch.
- **`skills`**: names of skill directories copied from the host's
  `~/.claude/skills/<name>/` into the agent.

## Trust

Your own bottles and agents run as written. A repo's run only once this host
trusts their exact contents (`bot-bottle trust <name>`), and any change asks
again. Trust records live in `~/.bot-bottle/trusted-bottles.json`; what the
trust hash covers is in [architecture.md](architecture.md#state).

- **Repo bottles** are always named `./<name>`, so a repo bottle never shares
  a name with one of yours. Bare `bot-bottle` uses `./main` when the repo has
  one. The trust prompt lists every host and secret the bottle touches,
  including what it inherits through `extends`, so a cloned repo can't send a
  credential to another host without you seeing it first.
- **Repo agents** need trust when their `agent_provider` grants something
  (your login, a model host with a secret, or an image build), or when they
  run on one of your bottles that grants more than `basic`. A repo agent with
  the same name as one of yours replaces it, and the trust prompt says so.

## Forges

`bot-bottle forge connect <url>` lets bottles push to a Gitea or GitHub forge:
it checks a token, pins the forge's host key and writes a `gitea` or `github`
bottle. A bottle that
extends it grants repos in a line,
`git-gate: {repos: [gitea.example.com/acme/app:write]}`, each read-only unless
`:write`, with its own deploy key per run. Details in
[git-gate.md](git-gate.md#forges-and-one-line-grants).

A bottle written before forges, with each repo's own `url`,
`key: {provider: gitea, ...}` and `host_key`, no longer loads; its error says
how to convert it, and `bot-bottle bottles migrate <name>` does it as a new
version. See
[Migrating from the old format](git-gate.md#migrating-from-the-old-format).

## Suggesting a bottle

`bot-bottle bottles suggest "<task>"` proposes the bottle a task needs without
writing anything — see [bottles-suggest.md](bottles-suggest.md) for the full
reference.  `--write NAME` saves the proposal as a new host bottle, and
`--architect` uses the `bottle-architect` agent (your own harness, no sandbox)
instead of the default rules path.

## Versions

Every change to a bottle or agent under `~/.bot-bottle/` is a version, hand
edits included.

- `bot-bottle bottles history <name>` lists them; add a version to see what it
  changed.
- `bot-bottle bottles revert <name> <version>` restores one as a new version,
  taking effect at the next launch.
- `bot-bottle bottles migrate <name>` rewrites a bottle's old-format git-gate
  repos as forge grants, as a new version
  ([git-gate.md](git-gate.md#migrating-from-the-old-format)).

A repo's bottles are versioned by the repo itself. Where versions are kept is
in [architecture.md](architecture.md#state).

## Egress routes

`egress.routes` is the bottle's allowlist. Every host not on it is refused,
DNS-over-HTTPS resolvers included. A route has `host`, an optional `role`,
and an optional `inspect` block holding everything else. How the proxy
applies a route is in [gateway.md](gateway.md#egress-proxy).

| Field | Required | Description |
|---|---|---|
| `host` | yes | Hostname to allowlist. One entry per host in a bottle; a later bottle's entry for the host replaces an inherited one ([composing](#bottles)). |
| `blocked` | no | `true` takes an inherited `host` away; the entry then has only `host` and `blocked`. |
| `role` | no | Reserved for future use. The key is recognised but any value is currently rejected at load. Provider auth routes (e.g. Claude's `api.anthropic.com`) are injected automatically from the agent's `agent_provider`, not via `role`; a bottle route for such a host fails the launch. |
| `inspect` | no | `false` passes the TLS tunnel through untouched: no DLP, auth injection or path matching. Otherwise a mapping of the fields below. |
| `inspect.auth.scheme` | when `auth` present | `Bearer` or `token`. Injected by the proxy; the agent never sees the value. |
| `inspect.auth.token_ref` | when `auth` present | Env-var name holding the secret on the host. |
| `inspect.matches` | no | Array of `{paths, methods, headers}` filters. A request must match at least one entry (if any are given) to be forwarded. An inspected route with no `matches` and no gateway-injected auth (`token_env`/`preserve_auth`) is read-only: only `GET` and `HEAD` pass, so list the methods (e.g. `methods: [GET, POST]`) to allow writes. |
| `inspect.matches[].paths` | no | Array of `{type, value}`. `type` is `prefix` (default), `exact`, or `regex`. |
| `inspect.matches[].methods` | no | Array of HTTP method strings, e.g. `[GET, POST]`. |
| `inspect.matches[].headers` | no | Array of `{name, value, type}`. `type` is `exact` (default) or `regex`. |
| `inspect.outbound_detectors` | no | `false` disables outbound scanning; a list restricts it to named detectors (`token_patterns`, `known_secrets`). Omit for all. |
| `inspect.inbound_detectors` | no | Off unless listed: name the detectors to run on responses (`naive_injection_detection`). Omit or `false` for none. |
| `inspect.outbound_on_match` | no | What to do when an outbound token is detected: `supervise` (default for manifest routes; hold for operator approval), `redact` (scrub the value and forward), or `block` (hard 403). Agent-provider routes (e.g. `api.anthropic.com`) default to `redact`. |
| `inspect.preserve_auth` | no | `true` forwards the client's own `Authorization` header instead of stripping it (registries whose token dance fetches per-scope tokens). |
| `inspect.git.fetch` | no | `true` permits smart HTTP clone/fetch (`git-upload-pack`) for this host. Push (`git-receive-pack`) remains blocked; pushes go through [git-gate](git-gate.md). |

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

- **`supervise`** (default): the proxy queues an `egress-token-allow` proposal
  for the operator's `bot-bottle supervise` TUI and holds the request open
  until it is answered, or until `EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS` (default
  300) elapses, after which it fails closed. The operator never sees the raw
  token, only the host, method, path and a redacted snippet. Approving adds
  the value to an in-memory safelist for that bottle and that host, for the
  life of the egress proxy; the same value sent to another host is flagged
  again.
- **`redact`**: the matched value is scrubbed from the body, headers and path,
  and the request is forwarded. It fails closed if a match lands somewhere
  unredactable, like the hostname.
- **`block`**: a hard `403`.

Structural blocks (CRLF injection), canary hits and not-in-allowlist host
blocks are always hard `403`s regardless of policy
([gateway.md](gateway.md#outbound-dlp)).
