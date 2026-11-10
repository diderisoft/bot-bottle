# Threat model

The adversary is the agent: a model, or whatever prompt-injected it, running
with broad permissions inside its bottle. The operator and the host are
trusted.

Each mechanism below names the module that enforces it. Where a test
exercises it, the test is named too.

The end-to-end tests are in `tests/integration/test_sandbox_escape.py`.
[escape-attempts.md](escape-attempts.md) lists what they cover and on which
backends they run. A new attack class gets an entry there, and a regression
test, before this page changes.

## Backends

The isolation boundary depends on the backend
([backends.md](../reference/backends.md)).

| Backend | Each agent is | Network |
|---|---|---|
| **firecracker** (Linux with KVM) | a Firecracker microVM with its own kernel | A point-to-point TAP. A host nft table lets it reach only its gateway. |
| **macos-container** | an Apple container: a lightweight VM with its own kernel | One host-only network shared by all agents. Each guest's own nft firewall keeps them apart. A host pf anchor keeps them off the Mac. |
| **docker** | a container on the host kernel, under gVisor `runsc` when Docker has it | An `--internal` network per agent, shared only with the gateway. |

Docker is the weakest boundary.

---

## What we defend against

| # | Threat | Stopped by |
|---|---|---|
| 1 | Network exfiltration to unlisted hosts | network isolation, the exact-match allowlist |
| 2 | DNS exfiltration | no route to public resolvers, gateway-side resolution |
| 3 | HTTP exfiltration through an allowlisted host | outbound DLP, on inspected routes only |
| 4 | Token-shaped secrets in a git push | gitleaks in git-gate's pre-receive hook |
| 5 | The bottle's own secrets in a git push | the held-secret scan |
| 6 | Credential theft from the agent | credentials added on the way out |
| 7 | Proxy bypass | network isolation |
| 8 | CONNECT tunnelling to an unlisted host | policy keyed on the `CONNECT` target |
| 9 | Git push over HTTPS, around git-gate | smart-HTTP refused on inspected routes |
| 10 | Unattributed supervise requests | attribution through the orchestrator |
| 11 | One agent reaching another | a VM or container per bottle, and network separation per backend |

Each row is detailed below.

### 1. Network exfiltration to unlisted hosts

**Threat:** the agent contacts a host not on its bottle's egress allowlist.
It may connect directly, by IP literal, by a subdomain of an allowed host, or
by naming an allowed host while connecting to another.

| Layer | What it does |
|---|---|
| Network isolation | The agent's network has no route off the host except to the gateway. This is per backend, as above. |
| Egress proxy allowlist | Policy keys on the host the proxy connects to, matched exactly and case-insensitively. A `CONNECT` to an unlisted host is refused before anything is tunnelled. |
| Host mismatch check | On inspected routes, a `Host` header, `:authority` or TLS SNI that differs from the connected host gets a 403. A `passthrough: true` route checks only the `CONNECT` target. |
| No raw TCP | Bytes after a `CONNECT` that aren't HTTP, or TLS carrying HTTP, are refused. |

**Code:** `gateway/egress/addon.py`, `gateway/egress/request_identity.py`,
`gateway/egress/matching.py`.

**Tested by:** `test_1_hostname_not_in_allowlist`,
`test_2_ip_not_in_allowlist`, `test_4_dns_exfil_blocked` (subTest
`crafted subdomain`).

---

### 2. DNS exfiltration

**Threat:** the agent encodes a secret in a DNS query.

- Network isolation: no route to public resolvers such as `8.8.8.8`.
- The resolver the agent is given doesn't resolve public names. The gateway
  resolves names itself.
- The exact-match allowlist refuses `$SECRET.api.anthropic.com`.

**Tested by:** `test_4_dns_exfil_blocked`, subTests `crafted subdomain`,
`direct dig` and `runtime resolver`.

---

### 3. HTTP exfiltration through an allowlisted host

**Threat:** the agent puts a secret in the path, query, body or a header of a
request to an *allowed* host.

These detectors run on inspected routes only. All of them run before
credentials are stripped and before any upstream contact.

| Detector | What it catches |
|---|---|
| Canary | The bottle's per-session canary, anywhere in the request. Always a block, never safelisted. |
| `known_secrets` | The upstream tokens the gateway injects for the bottle (`EGRESS_TOKEN_*`), in several encodings and split or fragmented forms. |
| `token_patterns` | Vendor token shapes: AWS, GitHub, Anthropic, OpenAI, Stripe, HuggingFace, Databricks, Slack, npm, SendGrid, PyPI, Vault, and long bearer JWTs. |
| CRLF injection | `\r\n` smuggled into outbound headers. Always on. |

On a token match, the route's `dlp.on_match` decides what happens.

| Policy | Default for | Does |
|---|---|---|
| `redact` | agent-provider routes | scrubs the value and forwards |
| `supervise` | other routes | holds the request for the operator. With no answer it blocks after 300 s. |
| `block` | | blocks |

Canary and CRLF hits are always a 403.

**Not covered:**

- A `passthrough: true` route is an unscanned tunnel. None of this applies
  to it.
- The bottle's own env values, secret or not, are in the agent's env. Egress doesn't
  block them, because they may legitimately go to their own host. The agent
  can send one to any allowlisted host.

**Code:** `gateway/egress/dlp.py`, `gateway/egress/dlp_detectors.py`.

**Tested by:** `test_6_held_secrets_never_leave` (subTest `canary to an
allowlisted host`), and the unit tests in `tests/unit/test_dlp_detectors.py`
and `tests/unit/test_egress_addon_request_flow.py`.

The end-to-end path, query, body and header test, `test_3_http_exfil_blocked`,
sends to an unlisted host. It exercises the allowlist, not DLP.

---

### 4. Exfiltration via git push (token-shaped secrets)

**Threat:** the agent commits a recognisable token, such as a GitHub, Slack or
GitLab one, and pushes it through git-gate.

**Mechanism:** git-gate's pre-receive hook runs gitleaks over the commits the
push introduces, before any upstream connection.

- A finding refuses the push.
- A finding hidden only by a `# gitleaks:allow` comment becomes a supervise
  request.

**Tested by:** `test_5_readme_push_blocked`, subTests `github`, `slack` and
`gitlab`. It also checks the refusal comes before any upstream network error.

---

### 5. Exfiltration via git push (the bottle's own secrets, any shape)

**Threat:** the agent commits its canary, or a secret with no known shape,
such as one of its `secret:` env values.

**Mechanism:** the held-secret scan (`gateway/git_gate/held_secret_scan.py`)
looks for the bottle's registered values. It runs after gitleaks and before
the upstream forward. A scan that can't run refuses the push.

| It looks for | It scans |
|---|---|
| the bottle's injected tokens | ref names |
| its canary | commit messages and identities |
| its `secret:` env values of at least 8 characters | diff paths and added lines |
| | annotated tag bodies |

**Tested by:** `test_6_held_secrets_never_leave`, subTests `push secret env
value` and `push canary`.

---

### 6. Credential theft from the agent

**Threat:** the agent reads an upstream credential and sends it somewhere.

**Mechanism:** credentials a route injects (`auth`) and forge credentials
never enter the agent.

- The gateway adds route credentials on the way out.
- git-gate holds the forge tokens and deploy keys.
- The agent sees placeholders, such as `ANTHROPIC_API_KEY=egress-placeholder`.
- On inspected routes, the gateway also strips credentials the agent adds
  itself. A key the agent was handed can't act on an allowlisted host.

**Not covered:** values a bottle passes as env (`secret:` entries, and `?`
and `${VAR}` inputs) are in the agent's env in plain text. git-gate refuses
the `secret:` ones in a push. Egress doesn't
block them (see 3).

See [secrets.md](../reference/secrets.md) for where each kind of secret is held.

---

### 7. Proxy bypass

**Threat:** the agent ignores the proxy (`--noproxy '*'`) or dials raw TCP.

**Mechanism:** network isolation. Off the gateway, the agent's network has no
route off the host.

**Tested by:** `test_2_ip_not_in_allowlist`, subTests `direct IP`, `proxy
bypass` and `CONNECT to an unlisted host's other port`.

---

### 8. CONNECT tunnelling to an unlisted host

**Threat:** the agent opens a tunnel to an unlisted host while naming an
allowed one. The request inside would then be policed, and given credentials,
as the allowed host.

**Mechanism:** policy keys on the `CONNECT` target.

- A tunnel to an unlisted host gets a 403 before it opens.
- On inspected routes, a `Host` or SNI that differs from the target gets a 403.

**Tested by:** `test_2_ip_not_in_allowlist`, subTests `CONNECT to unlisted
host, Host names a listed one` and `plain HTTP to unlisted host, Host names a
listed one`.

---

### 9. Git push over HTTPS, around git-gate

**Threat:** the agent pushes straight to a forge over HTTPS.

**Mechanism:** on inspected routes, the egress proxy refuses smart-HTTP push
(`git-receive-pack`) on any host. It refuses fetch too, unless the route sets
`git: {fetch: true}`.

**Not covered:**

- A `passthrough: true` route isn't decrypted, so the proxy can't see a push
  on it.
- This blocks the git protocol, not writes. An allowlisted forge API route
  with gateway `auth` can still change repos within its `methods`, `allow`
  and the token's scopes. A contents API is one example.

---

### 10. Unattributed supervise requests

**Threat:** the agent calls a supervise tool, such as `egress-allow` or
`add-repo`, as another bottle or as none.

**Mechanism:** every supervise call is attributed by (source IP, identity
token) through the orchestrator.

- A call that resolves to no bottle is refused and queues nothing.
- The agent can't reach the orchestrator directly on any backend.
- Every request still needs the operator's answer.

---

### 11. One agent reaching another

**Threat:** one agent reads another's files, processes or traffic.

**Mechanism:** each bottle is its own VM (firecracker, macos-container) or
container (docker). It has its own filesystem and process tree. The gateway
resolves each bottle's own policy and tokens per request.

| Backend | Network between agents |
|---|---|
| firecracker | a point-to-point TAP per agent |
| docker | an `--internal` network per agent, shared only with the gateway |
| macos-container | one shared network, with each guest's own firewall. See the root limit below. |

**Tested by:** `test_multitenant_isolation.py`. It runs two bottles through
one gateway, each with only its own tokens and routes, on Docker.

---

## What we explicitly do NOT defend against

These are known gaps: architectural limits or deliberate scope decisions, not
bugs.

| Out-of-scope threat | Why |
|---|---|
| **Compromised host** | Root on the host, or the host user's own access, controls every bottle, the infra pair and all state. This is host security, not a sandbox problem. |
| **Compromised orchestrator** | It holds the control-plane signing key, the bottle registry and every bottle's egress tokens, encrypted at rest. Compromise allows minting `cli` and `gateway` tokens, changing allowlists and reading tokens. |
| **Compromised gateway** | It holds every running bottle's upstream credentials while it serves them, and the mitmproxy CA. Compromise gives all of that. It is the most valuable component to protect. |
| **Compromised or hostile console** | See [The console](#the-console) below. Pairing gives the console operator-level control. `bot-bottle pair off` turns the link off. |
| **Audit journal rewritten on the host** | The hash chain has no key. Whoever has the host user's access can rewrite the whole chain, or cut its tail, and it still verifies. See [The audit journal](#the-audit-journal) below. |
| **Escape from the VM or container** | A kernel, hypervisor or container-runtime exploit is out of scope. See [VM and container escape](#vm-and-container-escape) below. |
| **Root inside a macos-container agent** | An agent with root in its VM can remove its in-guest firewall and spoof other agents. pf still keeps it off the Mac. See [Root inside a macos-container agent](#root-inside-a-macos-container-agent) below. |
| **Malicious or backdoored agent image** | Runtime controls assume the image is trustworthy. Base images are digest-pinned. A supply-chain compromise upstream of that pin isn't detected. |
| **Side channels** (timing, cache, Spectre-class) | Agents share physical hardware. Cross-agent side channels aren't prevented. |
| **Unregistered or shapeless secrets** | gitleaks needs a shape or keyword. The held-secret scan only knows the bottle's registered values, of 8 characters or more. `?` and `${VAR}` inputs aren't registered. A secret the bottle wasn't given, with no known shape, passes. |
| **`passthrough: true` routes** | Unscanned tunnels: no DLP, canary check, credential stripping, git push block or SNI check. Anything the agent holds can go to that host, or to other names a server there answers for. |
| **What an allowlisted upstream does** | Once a request passes the gateway, bot-bottle doesn't control what the upstream does with it. That includes DNS-over-HTTPS, or a forge API writing to repos. |
| **Policy changes on open connections** | HTTPS policy is resolved once per `CONNECT` and reused for that connection. A change, such as an approval or a removed route, takes effect on the next connection, not on one already open. |
| **Source-IP spoofing** | Attribution is (source IP, identity token). It relies on network isolation for the source IP, not on cryptographic proof of source. See the macos-container row. |

### The console

Pairing gives the console operator-level control. The console is
`https://bot-bottle.app` unless you paired with another.

| A paired console can | It can't |
|---|---|
| launch agents | read a secret's value over the link |
| answer requests | launch agent content of its own |
| widen egress | decrypt the audit records it stores without a passkey-signed unlock from a passkey this host trusts |
| rewrite host bottles, which run with no trust prompt | |
| change and re-trust project bottles | |
| set host secrets, so it can route a host secret to a host of its choosing | |

`bot-bottle pair off` turns the link off. See
[host-controller.md](../reference/host-controller.md#what-pairing-lets-the-console-do).

### The audit journal

The journal's hash chain has no key, and its files are ordinary files of the
host user.

- It catches accidental damage and partial edits.
- Whoever has the host user's access can rewrite the whole chain, or cut its
  tail, and it still verifies.
- Tamper evidence against that needs the records forwarded off the host
  (`bot-bottle audit forward`). Forwarding is off by default.

See [host-controller.md](../reference/host-controller.md#audit).

### VM and container escape

A kernel, hypervisor or container-runtime exploit is out of scope.

- On docker the agent shares the host kernel. gVisor narrows that when
  installed.
- On firecracker and macos-container an escape would need a VM escape.
- Beyond dropping `NET_RAW` (docker, macos-container), bot-bottle adds no
  seccomp or AppArmor profile of its own. The runtime's defaults apply.

### Root inside a macos-container agent

An agent that gets root in its VM can:

- remove its in-guest firewall
- reach agents that have no firewall
- change its address and spoof others on the shared agent network

pf still keeps it off the Mac. A root-capable agent is refused at launch when
other agents are running. The check catches images built to allow root, not
exploits.

---

## Mechanism-to-module reference

| Mechanism | Module |
|---|---|
| Egress proxy and allowlist | `bot_bottle/gateway/egress/addon.py` |
| Route matching | `bot_bottle/gateway/egress/matching.py` |
| Route policy and credential stripping | `bot_bottle/gateway/egress/request_pipeline.py` |
| Host mismatch check | `bot_bottle/gateway/egress/request_identity.py` |
| DLP dispatch and canary check | `bot_bottle/gateway/egress/dlp.py` |
| Token-pattern, known-secret and CRLF detectors | `bot_bottle/gateway/egress/dlp_detectors.py` |
| git-gate pre-receive and gitleaks | `bot_bottle/gateway/git_gate/` |
| Held-secret scan | `bot_bottle/gateway/git_gate/held_secret_scan.py` |
| Commit trailers (attribution, not secret protection) | `bot_bottle/gateway/git_gate/trailer_check.py` |
| Supervise MCP server | `bot_bottle/gateway/supervisor/server.py` |
| Policy resolution and attribution | `bot_bottle/gateway/policy_resolver.py` |
| Console link | `bot_bottle/control/hostctl/remote.py`, `remote_actions.py` |
| Audit journal | `bot_bottle/control/hostctl/audit_journal.py`, `audit_verify.py`, `audit_forward.py` |

---

## Regression corpus

[escape-attempts.md](escape-attempts.md) lists each attack the end-to-end
suite tries, the test that runs it, where the suite runs, and what it doesn't
cover yet.
