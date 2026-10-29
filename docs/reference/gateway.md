# Gateway

The per-host data plane, and the only way out for an agent. One gateway serves
every bottle and applies each bottle's own policy, which it resolves per
request (see [Attribution](architecture.md#attribution)). It holds the
mitmproxy CA and a `gateway` token for the control plane, never a signing key.
It keeps no durable per-bottle state.

## Processes

`gateway/bootstrap.py` is PID 1. It starts each daemon, prefixes its log lines
with the daemon's name, restarts a daemon that dies, and forwards
SIGTERM/SIGINT. `BOT_BOTTLE_GATEWAY_DAEMONS` limits which daemons run.

| Daemon | Port | Code |
|---|---|---|
| `egress` (mitmdump + addon) | 9099 | `gateway/egress/` |
| `git-http` (git-gate's smart-HTTP server) | 9420 | `gateway/git_gate/`; see [git-gate.md](git-gate.md) |
| `supervise` (MCP server) | 9100 | `gateway/supervisor/` |

Only `egress` gets the egress token env. Every daemon reaches the orchestrator
through `PolicyResolver` (`gateway/policy_resolver.py`).

## Egress proxy

The agent's `HTTP_PROXY` and `HTTPS_PROXY`. The identity token is the password
in `Proxy-Authorization`. For HTTPS it is captured at `CONNECT` and remembered
per client connection, then stripped so it never goes upstream. A `CONNECT`
with no token that resolves to no bottle gets a `407` with `Proxy-Authenticate:
Basic`, so a client that waits to be challenged (git's default
`http.proxyAuthMethod=anyauth`) sends its proxy URL's credentials and retries.

### Per request

`EgressAddon.request` in `gateway/egress/addon.py`:

1. **Resolve** the bottle's policy, slug and env from (source IP, token).
   HTTPS resolves once at `CONNECT` and reuses the result.
2. **Host check.** Policy keys on the host mitmproxy connects to
   (`request.host`: the `CONNECT` target or the absolute URL's host). A
   `Host` header, `:authority` or TLS SNI naming any other host gets a 403,
   so a request can't pass as an allowlisted host while going elsewhere.
3. **Introspection.** `_egress.local/allowlist` returns the calling bottle's
   own routes.
4. **Outbound DLP**, on routes with `inspect` enabled. Runs before auth is
   stripped, so a token smuggled in any header, the path, the query, the body
   or the hostname is caught. The result is handled by the route's
   [on-match policy](#outbound-dlp).
5. **Git boundary.** Smart-HTTP push to a forge is always blocked; fetch only
   when the route sets `git.fetch`. Pushes go through git-gate.
6. **Route policy** (`request_pipeline.evaluate_route_policy`). A host not on
   the allowlist, or a request matching no `matches` entry, gets a 403. The
   agent's `Authorization` is stripped unless the route sets `preserve_auth`,
   and the route's `auth` is injected from the bottle's egress tokens.
7. **First contact.** The first request from a bottle to a host is written to
   the audit journal. With `audit.delivery: required`, the request waits for
   that record and is refused if it can't be written.

Responses on inspected routes go through the inbound detectors before they
reach the agent.

A `CONNECT` to a host with no route is refused before anything is
tunnelled. The addon sets mitmproxy's `rawtcp` off, so bytes after a
`CONNECT` that aren't HTTP (or TLS carrying HTTP) are refused rather than
relayed as raw TCP.

Routes with `inspect: false` are not TLS-intercepted. The allowlist decision
happens at `CONNECT`, and the TLS tunnel passes through untouched: no DLP, no
auth injection, no path matching. Only TLS passes: a non-TLS stream on such a
route is refused like any other raw TCP.

### Outbound DLP

Detectors are in `gateway/egress/dlp_detectors.py`. Routes enable them by name
under `dlp.outbound_detectors` and `dlp.inbound_detectors`:

| Detector | Direction | Catches |
|---|---|---|
| `token_patterns` | outbound | vendor token shapes |
| `known_secrets` | outbound | the bottle's own provisioned values in several encodings, including split or fragmented forms |
| `entropy` | outbound | high-entropy blobs; warns only, and only on routes that list it |
| `naive_injection_detection` | inbound | prompt-injection phrasing in responses |
| (always on) | outbound | CRLF header injection |

The bottle's env also carries a per-session **canary** secret. Any outbound
appearance of it is an exfiltration attempt, and the request is always
blocked.

`dlp.outbound_on_match` decides what a token match does:

| Policy | Default for | Does |
|---|---|---|
| `supervise` | manifest routes | holds the request and queues an `egress-token-allow` proposal. Approval adds the value to that bottle's safelist for the proxy's life and re-scans. Times out closed after `EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS` (300). If supervise isn't wired for the bottle, it blocks. |
| `redact` | agent-provider routes | scrubs the value and forwards. Fails closed if a match can't be removed, for example in the hostname. |
| `block` | | 403 |

Canary hits, CRLF injection and allowlist misses are always a 403, whatever
the policy.

### Route schema

`gateway/egress/schema.py` parses the policy blob the orchestrator stores.
That blob is rendered from the bottle's `egress.routes` by
`control/registration.py`. Field reference is in the README's manifest
section.

## Supervise

`gateway/supervisor/server.py` is an MCP server the agent calls when it's
stuck. Its tools are `egress-allow`, `egress-block`, `list-egress-routes`,
`forge-connect`, `add-repo` and `check-proposal`. Its `initialize` instructions and the egress tool
descriptions say that git never goes out through egress, even to allowed
hosts: the bottle's repos go through git-gate, and a repo it doesn't have
needs the operator to add it. The same note is in every agent's prompt
(`contract/agent_notes.py`, [ADR 0003](../decisions/0003-system-prompts-stay-user-directed.md)).

A proposal is queued with the orchestrator (`POST /supervise/propose`), which
attributes it to the calling bottle. The server polls for up to
`SUPERVISE_RESPONSE_TIMEOUT_SECONDS` (30). If there's no decision by then, it
returns `pending` with the proposal id, and `check-proposal` picks up the
answer later. An agent told `pending` usually stops and waits, so when the
host answers a request older than that wait (the phone, the terminal's popup,
or a forge or repo card), it types a line saying so into the agent's session
(`control/hostctl/nudge.py`). The operator decides in `bot-bottle supervise`, the terminal's
request popup, or the console. The orchestrator applies approved egress
changes to the bottle's stored policy, so the next request resolves the new
routes. The gateway never opens `bot-bottle.db`.

An approved egress addition can also be kept (`approvals.py`): the terminal's
`[k]`, or the phone's second option, writes its routes into the bottle as
well, from the next launch. It goes to the project's `./main` when the bottle
shared a project (trusted, left for you to commit), else to the first host
bottle it launched with, as a version naming the request
([PRD 0103](../prds/0103-forge-connect-and-bottle-onboarding.md)). A route
carrying a credential can only be allowed once.

`forge-connect(url, justification)` asks the operator to connect a git forge
(an https URL). It's answered from the console, whose card shows the URL (marked when
no host bottle declares that forge yet), the forge's token page and the scopes
needed. The operator pastes a token there; the host connects the URL from the
agent's proposal, never one from the console, exactly as `bot-bottle forge
connect` does, then approves the proposal with a note naming the account. A
failed connect leaves it waiting. The token never reaches the agent
([PRD 0105](../prds/0105-forges-and-repos-from-the-phone.md)).

`add-repo(repo, justification)` asks for a repo as a grant,
`<forge>/<owner>/<repo>[:write]`. The console's card shows it with the access
asked for. The operator can grant read-only instead of write, and can keep the
grant in the bottle (the same target as a kept egress route; it also extends
the forge's bottle when the chain doesn't). Granting adds the repo to the
running bottle ([git-gate](git-gate.md#live-grants)) and approves with a note
giving its URL.

Neither request can be plainly allowed (`approvals.answer` refuses): that
would tell the agent it has what it asked for. The terminal can only deny them.

A proposal takes one decision: the first answer stands. A later answer, such
as the phone answering after the terminal did, gets a 409 (`already
answered`), changes nothing, and is recorded as a `decision.late` audit event
naming the decision it lost to, so both answers are on record.

Proposals that git-gate raises (`gitleaks-allow`) and the egress proxy raises
(`egress-token-allow`) use the same queue.

## Request firehose

`gateway/egress/firehose.py` ([PRD 0093](../prds/0093-request-firehose.md)).
When a consumer is attached, such as `bot-bottle audit tail` or a configured
sink, the proxy snapshots each request as the agent sent it. It drops
`Authorization`, `Proxy-Authorization` and `Cookie`, scrubs provisioned
secrets, and keeps the first `max_body_bytes` of the body plus a SHA-256 of
the whole. It sends one OCSF HTTP Activity record per request through the
orchestrator to the host controller.

The stream is lossy and never slows a request. With nothing listening, no
snapshot is taken. A bottle with `audit.firehose: metadata` sends only the
destination, decision, status, size and timing.

## Audit

Gateway daemons emit audit events as producers `egress`, `git-gate` and
`supervise` (`gateway/audit_client.py`). Events queue in memory and go to the
orchestrator, which relays them to the host controller's journal (see
[host-controller.md](host-controller.md#audit)). A full queue drops the oldest
event and records an `audit.truncation_detected` event once delivery resumes.

## CA

The mitmproxy CA lives in a host directory mounted at mitmproxy's confdir, so
it survives the gateway being recreated. `python -m bot_bottle.control.rotate_ca`
(which runs `gateway/ca_rollover.py` in the gateway) mints a new CA
cross-signed by the old one, then restarts each gateway and reattaches its
bottles. The gateway serves `leaf → new CA (issued by
old) → old CA`, so an agent that loaded the old CA keeps working and new
processes trust the new one
([PRD 0090](../prds/0090-gateway-ca-rollover.md)).
