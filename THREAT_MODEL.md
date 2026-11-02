# Bot-Bottle Threat Model

This document enumerates what bot-bottle defends against, what it explicitly does not
defend against, and which mechanism defends against which class of attack. Every claim
here is backed by a regression test or a reference to the module that enforces it.

This is a living document. Each new attack class gets a dated entry in
[`docs/security/escape-attempts.md`](docs/security/escape-attempts.md) and a
corresponding regression test before this document is updated.

---

## What We Defend Against

### 1. Network exfiltration to unlisted hosts

**Threat**: Agent attempts to contact a host not in its per-bottle egress allowlist —
directly, via IP literal, via DNS subdomain, or via Host-header spoofing.

**Mechanisms** (in order of intercept):

| Layer | What it does |
|---|---|
| Network isolation | Agent namespace has no default gateway. No route off-host exists. |
| Egress proxy allowlist | Exact case-insensitive match on actual connection target, not on `Host` header or SNI. |
| Host mismatch detection | If `Host` header, `:authority`, or TLS SNI diverges from the CONNECT target, request is blocked before route evaluation. |
| Subdomain variant rejection | Allowlist uses exact match; `secret.api.anthropic.com` is not a match for `api.anthropic.com`. |

**Tested by**: `test_sandbox_escape.py::test_non_allowlisted_hostname`,
`test_non_allowlisted_ip_*`, `test_host_header_spoof_*`

---

### 2. DNS exfiltration

**Threat**: Agent encodes a secret in a DNS query to an external resolver.

**Mechanisms**:

- Network isolation: no route to external resolvers (e.g., `8.8.8.8`).
- Internal resolver has no upstream; public names do not resolve inside the agent.
- Exact-match allowlist blocks `$SECRET.api.anthropic.com`-style subdomain abuse before
  any DNS query is issued.

**Tested by**: `test_sandbox_escape.py::test_dns_exfil_*`

---

### 3. HTTP exfiltration through an allowlisted host

**Threat**: Agent embeds a secret in the URL path, query string, request body, or a
header of a request to an *allowed* host.

**Mechanisms** (all run before Authorization stripping and before upstream contact):

| Detector | What it catches |
|---|---|
| **Canary detector** | Per-session random value (`HELD_CANARY`). Always hard-block; not safelisted. Triggers on `HELD_CANARY` in any text position. |
| **Known-secrets detector** | Exact-value match on all `HELD_*` and `EGRESS_TOKEN_*` secrets from the bottle's env, with Unicode NFKD normalization to catch fragmented or encoded forms. |
| **Token-patterns detector** | Regex patterns for AWS, GitHub (`ghp_*`, `github_pat_*`), Anthropic (`sk-ant-*`), OpenAI (`sk-*`), GitLab (`glpat-*`), Slack (`xoxb-*`), Stripe, HuggingFace, npm, SendGrid, PyPI, Vault, and others. |
| **CRLF injection detector** | Always-on. Blocks `\r\n` in outbound headers. |

On a DLP match the default policy per route type is:

- Agent-provider routes (`api.anthropic.com` etc.): **redact** (scrub value and forward).
- All other inspected routes: **supervise** (queue `egress-token-allow` proposal; block
  after 300s timeout if no decision).
- Canary and CRLF matches: always **block** regardless of route policy.

**Tested by**: `test_sandbox_escape.py::test_http_exfil_*`, `test_canary_*`

---

### 4. Exfiltration via git push (known-shape secrets)

**Threat**: Agent commits a secret with a recognisable pattern (GitHub token, Slack
token, etc.) and pushes to a connected forge via git-gate.

**Mechanism**: Pre-receive hook runs `gitleaks git` on only the new commits before any
upstream SSH connection attempt. A gitleaks finding refuses the push.

**Timing guarantee**: The rejection must not contain upstream error phrases
("could not resolve", "connection refused", "host key verification failed"). If it does,
gitleaks ran late.

**Tested by**: `test_sandbox_escape.py::test_gitleaks_secret_in_push_*`

---

### 5. Exfiltration via git push (bottle's own secrets, any shape)

**Threat**: Agent commits a shapeless secret (high-entropy hex, custom token with no
gitleaks rule) or the per-session canary value.

**Mechanisms**:

1. **Commit trailer check**: every new commit must carry `Bot-Bottle: <bottle>/<run>`.
   Pushes without it are refused before scanning.
2. **Held-secret scan** (`held_secret_scan.py`): exact-value match on all `HELD_*` and
   `EGRESS_TOKEN_*` secrets from the bottle's env, with NFKD normalisation. Scans ref
   names, commit messages, author/committer identity, diff paths, and all added lines.
   Runs after gitleaks, before the upstream SSH forward.
3. **Canary**: `HELD_CANARY` is included in the held-secret scan; also detected by the
   egress DLP on any HTTP request to an allowed host.

Fail-closed: a scan error (exit 2) refuses the push.

**Tested by**: `test_sandbox_escape.py::test_shapeless_secret_in_push`,
`test_canary_in_push`, `test_canary_to_allowed_host`

---

### 6. Credential theft from the agent's environment

**Threat**: Agent reads its `EGRESS_TOKEN_*` or `HELD_*` env vars and attempts to
exfiltrate them.

**Mechanism**: Credentials are injected into the agent's env by the gateway; the agent
cannot read their plaintext values through any other channel. DLP and held-secret scan
treat these values as secrets (above). The agent has no access to the key material used
to derive them.

---

### 7. Proxy bypass (no-proxy flag, direct TCP)

**Threat**: Agent uses `--noproxy '*'` or dials a raw TCP connection to bypass the
egress proxy.

**Mechanism**: Network isolation. The agent's network namespace has no default route.
Direct connections cannot reach the internet regardless of proxy settings. The egress
proxy is the sole exit.

**Tested by**: `test_sandbox_escape.py::test_noproxy_bypass`, `test_direct_ip_connect`

---

### 8. HTTPS CONNECT tunnelling to an unlisted host

**Threat**: Agent sends a CONNECT request to the proxy using an allowed host but routes
the inner TLS session to a different destination.

**Mechanism**: The proxy keys policy on the actual CONNECT target, not on the SNI or
Host header inside the tunnel. SNI is checked against the connection target; mismatch
→ 403 before TLS.

**Tested by**: `test_sandbox_escape.py::test_connect_tunnel_spoof`

---

### 9. Git HTTPS push to arbitrary host

**Threat**: Agent pushes directly to a git host via HTTPS (bypassing git-gate).

**Mechanism**: The egress proxy detects smart-HTTP git push handshakes on any host and
blocks them unconditionally with an error message directing the agent to use git-gate.
Git fetch over HTTPS is similarly blocked unless the route explicitly sets
`git: {fetch: true}`.

---

### 10. Unauthorised supervise tool use

**Threat**: Agent calls a supervise tool (e.g., `egress-allow`) without being attributed
to a known bottle.

**Mechanism**: Every request to the supervise server is attributed by (source IP,
identity token) pair via orchestrator resolution. Unknown pairs return 407 (challenge) or
403 (deny). The orchestrator is unreachable from inside the agent.

---

### 11. Agent accessing another agent's resources

**Threat**: One agent reads another agent's files, memory, or network traffic.

**Mechanism**: Each bottle runs in an isolated container with its own network namespace,
filesystem, and process tree. Agents share no network segment. Backend enforces
namespace isolation.

---

## What We Explicitly Do NOT Defend Against

These are known gaps. They are not bugs — they are architectural limits or deliberate
scope decisions. Each one represents a class of attack a sufficiently privileged or
sophisticated adversary could exploit.

| Out-of-scope threat | Why |
|---|---|
| **Compromised host** | An attacker with root on the host running bot-bottle can escape any container. This is a host-security problem, not a sandbox problem. |
| **Compromised orchestrator** | The orchestrator holds signing keys and policy. Compromise allows forging tokens and modifying allowlists. Orchestrator hardening is separate. |
| **Malicious or backdoored agent base image** | Runtime controls assume the image was built from trusted sources. Supply-chain compromise of the image is not detected. |
| **Side-channel attacks** (timing, cache, Spectre-class) | Agents share physical hardware. Cache-timing exfil between co-located agents is not prevented. |
| **Low-entropy / no-keyword secrets not registered as HELD_*** | gitleaks requires pattern or entropy signal. Custom secrets with no keyword context escape gitleaks detection; held-secret scan only catches secrets that were registered as `HELD_*` at launch time. |
| **DNS-over-HTTPS tunnelling on an allowed host** | If an agent uses DoH via an allowlisted host, the request passes route policy. The canary and DLP detectors would catch an attempt to exfil a registered secret, but arbitrary DNS-over-HTTPS queries are not blocked. |
| **Compromised allowlisted upstream** | Once a request passes the gateway, bot-bottle does not control what the upstream does with it. A compromised upstream may log credentials, inject content, or leak data. |
| **TOCTOU on policy** | Policy is resolved per request. A policy change approved while a connection is in flight takes effect on the next request. |
| **IP-spoof identity replay** | Attribution uses (source IP, token) pair. IP spoofing within a shared network segment could replay a token. Mitigated by network isolation but not by cryptographic proof of source. |
| **Root escalation inside the container** | If the agent achieves root inside its container via a kernel vulnerability, namespace escape becomes possible. Defence-in-depth (non-root base images, seccomp, AppArmor) is recommended at the deployment layer but not enforced by bot-bottle itself. |

---

## Mechanism-to-Module Reference

| Mechanism | Module |
|---|---|
| Egress proxy allowlist + DLP | `bot_bottle/gateway/egress/` |
| Per-route policy evaluation | `bot_bottle/gateway/egress/matching.py` |
| Canary detector | `bot_bottle/gateway/egress/dlp.py` |
| Token-patterns detector | `bot_bottle/gateway/egress/dlp.py` |
| Known-secrets detector | `bot_bottle/gateway/egress/dlp.py` |
| CRLF injection detector | `bot_bottle/gateway/egress/dlp.py` |
| Git-gate pre-receive hook | `bot_bottle/gateway/git_gate/` |
| gitleaks integration | `bot_bottle/gateway/git_gate/` |
| Held-secret scan | `bot_bottle/gateway/git_gate/held_secret_scan.py` |
| Commit trailer enforcement | `bot_bottle/gateway/git_gate/trailer_check.py` |
| Supervise MCP server | `bot_bottle/gateway/supervisor/server.py` |
| Supervise dispatch | `bot_bottle/gateway/supervisor/mcp_dispatch.py` |
| Policy resolution + attribution | `bot_bottle/gateway/policy_resolver.py` |

---

## Regression Test Corpus

See [`docs/security/escape-attempts.md`](docs/security/escape-attempts.md) for the
dated log of attack techniques attempted against bot-bottle, each cross-referenced to
the test that catches it.

The full test suite lives in `tests/integration/test_sandbox_escape.py`. All escape
tests must pass before any release.
