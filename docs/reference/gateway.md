# Gateway

The gateway is the per-host data plane, and the only way out for an agent.
One gateway serves every bottle. It resolves each request to its bottle and
applies that bottle's own policy (see [Attribution](architecture.md#attribution)).

What it holds:

- the mitmproxy CA
- a `gateway` token for the control plane, never a signing key
- while it serves a bottle, that bottle's upstream credentials: the egress
  tokens it injects, resolved from the orchestrator, and git-gate's forge
  tokens and deploy keys

It keeps no durable per-bottle state. Even so, it is the most valuable
component to protect. A compromised gateway has every running bottle's
upstream access.

## Processes

The gateway runs three daemons under one PID 1.

| Daemon | Port | Code |
|---|---|---|
| `egress` (mitmdump + addon) | 9099 | `gateway/egress/` |
| `git-http` (git-gate's smart-HTTP server) | 9420 | `gateway/git_gate/`, see [git-gate.md](git-gate.md) |
| `supervise` (MCP server) | 9100 | `gateway/supervisor/` |

PID 1 is `gateway/bootstrap.py`. It starts each daemon, prefixes its log
lines with the daemon's name, restarts a daemon that dies, and forwards
SIGTERM/SIGINT. `BOT_BOTTLE_GATEWAY_DAEMONS` limits which daemons run.

Only `egress` gets the egress token env. Every daemon reaches the
orchestrator through `PolicyResolver` (`gateway/policy_resolver.py`).

## Egress proxy

The egress proxy is the agent's `HTTP_PROXY` and `HTTPS_PROXY`. It knows each
bottle by its identity token.

- The identity token is the password in `Proxy-Authorization`.
- For HTTPS, the token is captured at `CONNECT` and remembered per client
  connection. Then it is stripped, so it never goes upstream.
- A `CONNECT` with no token that resolves to no bottle gets a `407` with
  `Proxy-Authenticate: Basic`. A client that waits to be challenged then
  sends its proxy URL's credentials and retries.

git's default `http.proxyAuthMethod=anyauth` is such a client.

### Per request

`EgressAddon.request` in `gateway/egress/addon.py` runs these steps:

1. **Resolve** the bottle's policy, slug and env from (source IP, token).
   HTTPS resolves once at `CONNECT` and reuses the result.
2. **Host check.** A `Host` header, `:authority` or TLS SNI naming a host
   other than the one mitmproxy connects to gets a 403.
3. **Introspection.** `_egress.local/allowlist` returns the calling bottle's
   own routes.
4. **Outbound DLP**, on inspected routes only, not `passthrough: true`.
5. **Git boundary.** Smart-HTTP push (`git-receive-pack`) is always blocked
   on inspected routes. Fetch is blocked unless the route sets `git.fetch`.
6. **Route policy.** Requests the allowlist doesn't permit get a 403. Agent
   credentials and cookies are stripped, then the route's `auth` is injected.
7. **First contact.** The first request from a bottle to a host is written to
   the audit journal.

Responses on inspected routes go through the inbound detectors the route
names before they reach the agent. None run by default.

A `CONNECT` to a host with no route is refused before anything is tunnelled.
The addon sets mitmproxy's `rawtcp` off. Bytes after a `CONNECT` that aren't
HTTP, or TLS carrying HTTP, are refused rather than relayed as raw TCP.

#### Details

**Host check.** Policy keys on the host mitmproxy connects to, `request.host`.
That is the `CONNECT` target or the absolute URL's host. So a request can't
pass as an allowlisted host while going elsewhere.

On a `passthrough: true` route only the `CONNECT` target is checked (see
[Routes with `passthrough: true`](#routes-with-passthrough-true)).

**Outbound DLP** runs before auth is stripped. So a token smuggled in any
header, the path, the query, the body or the hostname is caught. The result
is handled by the route's [on-match policy](#outbound-dlp).

**Git boundary.** Pushes go through git-gate. This blocks the git protocol,
not writes.

An allowlisted forge *API* route with gateway `auth` or `preserve_auth` can
still change repos, for example through a contents API. It can do as much as
its `methods`, `allow` and the token's scopes allow. Grant forge API routes
deliberately.

**Route policy** is `request_pipeline.evaluate_route_policy`:

- A host not on the allowlist gets a 403. So does a request matching no
  `allow` entry.
- An inspected route that lists no `methods`, on the route or in an `allow`
  entry, is read-only, with or without `auth`.
- On such a route, anything but `GET`/`HEAD` gets a 403 until the route lists
  the methods it may use (`["*"]` for any).
- The agent's [credentials](#agent-credentials) and any
  [cookies the host didn't set](#cookies) are stripped unless the route sets
  `preserve_auth`.
- Then the route's `auth` is injected from the bottle's egress tokens.

**First contact.** With `audit.delivery: required`, the request waits for the
first-contact record. It is refused if the record can't be written.

### Routes with `passthrough: true`

A route with `passthrough: true` is an unscanned channel to its host. Anything
the agent holds can go there.

| Check | Inspected route | `passthrough: true` route |
|---|---|---|
| Allowlist decision at `CONNECT` | yes | yes |
| TLS interception | yes | no, the tunnel passes through untouched |
| `Host`, `:authority` and SNI match the target | yes | no, only the `CONNECT` target is checked |
| Outbound DLP | yes | no |
| Canary check | yes | no |
| Credential and cookie stripping | yes | no |
| Git push block | yes | no |
| Auth injection | yes | no |
| `methods` and `allow` rules | yes | no |
| Non-HTTP raw TCP | refused | refused |

Because the TLS SNI and the `Host` inside the tunnel aren't checked, a server
that answers for other names can be reached as those names. A CDN is one
example.

Only TLS passes. A non-TLS stream on such a route is refused like any other
raw TCP.

### Agent credentials

On an inspected route without `preserve_auth`, the only identity that goes
upstream is the one the gateway injects, the route's `auth`.

Before injecting, the gateway strips every credential the agent put in the
request. An agent handed someone else's key, by a prompt injection for
example, can't act as that account on an allowlisted host.

| Where | Stripped |
|---|---|
| Headers | `Authorization`, `X-Api-Key`, `Api-Key`, `X-Goog-Api-Key`, `Private-Token`, `X-Auth-Token`, `X-Access-Token`, `X-Gitea-Token`, `X-Vault-Token`, `X-Amz-Security-Token` and `Ocp-Apim-Subscription-Key`, plus the header the route injects into |
| Query parameters | `access_token`, `token`, `key`, `api_key`, `apikey` and `private_token`, compared case-insensitively after URL-decoding |

- `allow` header filters see the request without them.
- The lists are `CREDENTIAL_HEADERS` and `CREDENTIAL_QUERY_PARAMS` in
  `gateway/egress/request_pipeline.py`.
- They are a denylist and can't be complete.
- `Proxy-Authorization` and the identity header belong to the gateway. They
  are always stripped before this.
- `passthrough` routes are tunnelled, so nothing is stripped there.

#### Injected auth

The route's `auth` is injected after stripping, so the gateway's value is the
one that goes out.

| Policy field | Manifest field |
|---|---|
| `auth_scheme` | `auth.type`: `bearer` is `Bearer`, `token` is `token` |
| `token_env` | `auth.value` (`secret:NAME` or `secret:${VAR}`) |
| `auth_header`, optional | `auth.name`, with `type: header` |

- With no `auth_header`, the token is sent as `Authorization: <scheme> <token>`.
- A `type: header` route puts the raw token in the header its `name` gives.
- `Host`, `Cookie`, `Proxy-Authorization` and the framing headers can't be
  auth headers.
- Outbound DLP doesn't scan the agent's value of the injected header, because
  it's replaced.

The Claude provider uses a named header for an Anthropic API key. With
`agent_provider.login: {api_key: secret:NAME}`, its `api.anthropic.com`
route injects the key raw into `x-api-key`.

The agent gets `ANTHROPIC_API_KEY=egress-placeholder`, hidden from the launch
summary. Its `~/.claude.json` pre-approves that placeholder.

Where these tokens are stored and who holds each one: see [secrets.md](secrets.md).

### Cookies

On an inspected route, a request's `Cookie` keeps only the cookies that host
set for this bottle.

The gateway sees every response on an inspected route. It remembers each
`Set-Cookie` name and value per (bottle, host) in `gateway/egress/cookie_jar.py`.

On a route without `preserve_auth`, a cookie pair the agent sends goes
upstream only if that host set the same name and value earlier. Any other
pair is dropped. If none are left, the header is removed.

A cookie the agent was handed, such as an attacker's session, never goes out.
Cookies the host set itself keep working.

Limits of the jar:

- It matches the exact host. A cookie set with a `Domain` attribute isn't
  sent to the domain's other hosts.
- A `Max-Age` of zero or less, or a past `Expires`, deletes the cookie.
- It ignores `Path` and `Secure`, which are left to the client.
- It keeps up to 256 cookies per (bottle, host) and drops the oldest beyond that.
- It's in memory, so after a gateway restart a host has to set its cookies again.

### Outbound DLP

DLP runs only on inspected routes. A `passthrough: true` route is an
unscanned channel: none of the detectors below, nor the canary check, sees
its traffic.

| Detector | Direction | Runs | Catches |
|---|---|---|---|
| `token_patterns` | outbound | unless the route's list leaves it out | vendor token shapes |
| `known_secrets` | outbound | unless the route's list leaves it out | the upstream tokens the gateway injects for the bottle (`EGRESS_TOKEN_*`) in several encodings, including split or fragmented forms |
| `entropy` | outbound | only when the route's list names it | high-entropy blobs. It warns only. |
| `naive_injection_detection` | inbound | only when the route names it | prompt-injection phrasing in responses |
| (always on) | outbound | every inspected route, whatever the list | CRLF header injection, and the canary |

Routes enable detectors by name under `dlp.outbound` and `dlp.inbound`. A
listed side runs exactly those. An omitted side keeps the defaults. The
detectors are in `gateway/egress/dlp_detectors.py`, and the defaults are
applied in `gateway/egress/dlp.py`.

- `dlp: false` turns every detector off. The canary and CRLF checks still run.
- `known_secrets` doesn't match the bottle's own env values, which may go
  to their own host.

`naive_injection_detection` is regex phrase matching on response bodies, so
it is trivially bypassed. It is kept as an opt-in signal rather than a default.

The allowlist and outbound DLP already stop what a successful injection would
try to do. That is reaching a host or sending a secret.

The bottle's env also carries a per-session **canary** secret. Any outbound
appearance of it on an inspected route is an exfiltration attempt, and the
request is always blocked.

`dlp.on_match` decides what a token match does:

| Policy | Default for | Does |
|---|---|---|
| `supervise` | manifest routes | holds the request and queues an `egress-token-allow` proposal |
| `redact` | agent-provider routes | scrubs the value and forwards. Fails closed if a match can't be removed, for example in the hostname. |
| `block` | | 403 |

Under `supervise`:

- Approval adds the value to that bottle's safelist for that host only, for
  the proxy's life, and re-scans.
- The same value sent to another host is flagged again.
- The request times out closed after `EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS` (300).
- If supervise isn't wired for the bottle, it blocks.

Canary hits and CRLF injection on inspected routes, and allowlist misses on
any route, are always a 403, whatever the policy.

### Logging

`egress.log` sets how much the proxy records ([manifest.md](manifest.md#egress-log)).

| Level | Records | Where |
|---|---|---|
| `blocked` | a summary line per blocked or flagged request | the proxy's log |
| `full` | also each inspected request and response in full | `/var/log/bot-bottle-egress/full.jsonl`, never the proxy's log |

Full records stay out of the log because on Firecracker the log is the VM's
serial console. A few busy bottles' bodies can saturate it.

`gateway/egress/full_log.py` writes the full log. `EGRESS_FULL_LOG_DIR`
overrides its directory.

- The file is created 0600.
- Each body is cut at 64 KiB.
- It rotates at 16 MiB, with three older files kept.

On the host, each microVM's console log is written through
`backend/firecracker/console_cap.py`. `console.log` is 0600 and rotates to
`console.log.1` at 8 MiB.

### Route schema

`gateway/egress/schema.py` parses the policy blob the orchestrator stores.
`control/registration.py` renders that blob from the bottle's `egress.routes`.
The manifest fields are in [manifest.md](manifest.md#egress-routes).

## Supervise

Supervise is an MCP server the agent calls when it's stuck
(`gateway/supervisor/server.py`).

Its tools are `egress-allow`, `egress-block`, `list-egress-routes`,
`forge-connect`, `add-repo`, `launch-bottle`, `create-repo` and
`check-proposal`. These four have their own sections below:

| Tool | Asks for | Answered from |
|---|---|---|
| [`forge-connect`](#forge-connect) | a forge connection | the console |
| [`add-repo`](#add-repo) | a repo grant | the console's card |
| [`launch-bottle`](#launch-bottle) | a child bottle | the console only. The terminal can deny it. |
| [`create-repo`](#create-repo) | a new repo | the console, or no card when `actions.create` allows it |

Its `initialize` instructions, the egress tool descriptions and every
agent's prompt say that git never goes out through egress, even to allowed
hosts. Repos go through git-gate with their usual URLs and no key or token.
A repo the bottle doesn't have needs the operator to add it (`contract/agent_notes.py`,
[ADR 0003](../decisions/0003-system-prompts-stay-user-directed.md)).

### Proposals

A proposal is queued with the orchestrator, which attributes it to the
calling bottle (`POST /supervise/propose`).

- The server polls for up to `SUPERVISE_RESPONSE_TIMEOUT_SECONDS` (30).
- With no decision by then, it returns `pending` with the proposal id.
- `check-proposal` picks up the answer later.

An agent told `pending` usually stops and waits. So when the host answers a
request older than that wait, it types a line saying so into the agent's
session (`control/hostctl/nudge.py`).

That covers answers from the phone, the terminal's popup, or a forge or repo
card.

The operator decides in `bot-bottle supervise`, the terminal's request popup,
or the console. The orchestrator applies approved egress changes to the
bottle's stored policy, so the next request resolves the new routes.

The gateway never opens a database.

**Kept approvals.** An approved egress addition can also be kept
(`approvals.py`). The terminal's `[k]`, or the phone's second option, writes
its routes into the bottle as well, from the next launch.

- When the bottle shared a project, it goes to the project's `./main`. The
  result is trusted, and left for you to commit.
- Otherwise it goes to the first host bottle it launched with, as a version
  naming the request (PRD 0103, design repo).
- A route carrying a credential can only be allowed once.

**No plain allow.** `approvals.answer` refuses to plainly allow
`forge-connect`, `add-repo`, `launch-bottle` or `create-repo`. That would tell
the agent it has what it asked for. The terminal can only deny them.

**One decision.** A proposal takes one decision: the first answer stands. A
later answer, such as the phone answering after the terminal did, gets a 409
(`already answered`) and changes nothing.

The late answer is recorded as a `decision.late` audit event naming the
decision it lost to. So both answers are on record.

**The record.** The audit journal is a decision's only record. A proposal is
`decision.proposed`, and its answer `decision.resolved`, joined by the
proposal id (`correlation_id`).

| Field of `decision.resolved` | Holds |
|---|---|
| `operator_action`, `operator_notes` | the answer and its notes |
| `untrusted.diff` | the policy change applied, as a unified diff. Only when an approval or modification changes egress policy. It carries the agent's proposed text, so it's a claim, and an export of a restricted record drops it. |
| `diff_truncated` | true when the diff was cut at 64 KiB |
| `diff_digest` | the whole diff's SHA-256 prefix, so a cut or dropped diff can still be matched |

For a bottle with `audit.delivery: required`, the record is written before
the change is applied. A decision that can't be recorded isn't applied.

git-gate's `gitleaks-allow` proposals and the egress proxy's
`egress-token-allow` proposals use the same queue.

### `forge-connect`

`forge-connect(url, justification)` asks the operator to connect a git forge,
Gitea or GitHub, given as an https URL.

Some forges are refused before the operator sees a card:

- A public host known to run another kind is refused before it's queued.
  The list is in `contract/forges.py`: gitlab.com, bitbucket.org, …
- Any other host is recognized by the host controller
  ([git-gate](git-gate.md#forges-and-one-line-grants)).
- One that isn't Gitea or GitHub is refused at once, and the agent is told so.

It's answered from the console, where the operator pastes a token. The host connects the URL from the agent's
proposal, never one from the console, exactly as `bot-bottle forge connect`
does.

Then the host approves the proposal with a note naming the account. A failed
connect leaves it waiting. The token never reaches the agent (PRD 0105,
design repo).

### `add-repo`

`add-repo(repo, justification)` asks for a repo as a grant,
`<forge>/<owner>/<repo>[:write]`.

A repo on a forge this host hasn't connected is refused at once
(`remote_actions.vet`). A forge of another kind says so. A Gitea or GitHub one
tells the agent to `forge-connect` it first.

The operator can grant read-only instead of the write asked for.

The operator can also keep the grant in the bottle, in the same place as a
kept egress route. The forge's description comes with it when the bottle
doesn't have one, never a token.

Granting adds the repo to the running bottle
([git-gate](git-gate.md#live-grants)). It approves with a note giving the
repo's URL.

### `launch-bottle`

`launch-bottle(agent, prompt, bottles, label, justification)` asks to start a
child bottle (PRD 0106).

The host checks names as a console launch does. It refuses:

- unknown agents or bottles
- a forge this host hasn't connected
- a child already at its parent's cap, `children.max`, default 3
- a nested launch. Depth is 1, so a child can't launch children of its own.

Access is compared with the parent's live elements. Anything beyond the
parent's is listed on the card as removed: egress hosts, repos, env names.

On approval the host launches the child with a trimmed run-bottle that holds
only within-parent elements. It records the parent's identity as the child's
`parent`, in its `bottled_agents` row.

### `create-repo`

`create-repo(repo, public, justification)` asks for a new repo,
`<forge>/<owner>/<name>`. It's private unless `public` is set, which queues it
with a `:public` suffix.

- The host refuses one it can't make, before a card.
- It creates one the bottle's `git_gate.actions.create` allows, without a card.
- Otherwise it asks on the console, with option `create_repo`
  (`remote_create_repo.py`).
- The card's Create sends `create_repo_answer`. Its reply says what was
  created, or why not.

Creating the repo also grants it with write
([git-gate](git-gate.md#created-repos)).

## Request firehose

When a consumer is attached, the proxy sends one OCSF HTTP Activity record per
request, through the orchestrator to the host controller.

Consumers include `bot-bottle audit tail` and a configured sink. The code is
`gateway/egress/firehose.py` (PRD 0093, design repo).

Each record snapshots the request as the agent sent it:

- `Authorization`, `Proxy-Authorization` and `Cookie` are dropped.
- Provisioned secrets are scrubbed.
- It keeps the first `max_body_bytes` of the body, plus a SHA-256 of the whole.

The stream is lossy and never slows a request. With nothing listening, no
snapshot is taken.

A bottle with `audit.firehose: metadata` sends only the destination, decision,
status, size and timing.

## Audit

Gateway daemons emit audit events as producers `egress`, `git-gate` and
`supervise` (`gateway/audit_client.py`).

Events queue in memory and go to the orchestrator. It relays them to the host
controller's journal (see [host-controller.md](host-controller.md#audit)).

A full queue drops the oldest event. It records an
`audit.truncation_detected` event once delivery resumes.

## CA

The mitmproxy CA lives in a host directory mounted at mitmproxy's confdir, so
it survives the gateway being recreated.

`python -m bot_bottle.control.rotate_ca` has the host controller rotate it
(`POST /orchestrator/rotate-ca`, a job whose lines it prints), since restarting
a gateway records infrastructure state only the host controller writes. The
rotation runs `gateway/ca_rollover.py` in the gateway:

1. It mints a new CA cross-signed by the old one.
2. It restarts each gateway and reattaches its bottles.

The gateway serves `leaf → new CA (issued by old) → old CA`. An agent that
loaded the old CA keeps working, and new processes trust the new one
(PRD 0090, design repo).
