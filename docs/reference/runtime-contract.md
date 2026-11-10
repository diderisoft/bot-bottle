# Runtime contract

Build against this contract, not against host code. It is what crosses the
boundary between the host and the services that run in guests: the
orchestrator's control plane and the host controller's API.

That applies to anything built against bot-bottle's services, such as an
operator's own tooling or a port. Each section names the `contract/` module
that is its reference implementation.

| Side | Status | Design |
|---|---|---|
| host ↔ orchestrator | covered here | PRDs 0087 and 0092 (design repo) |
| agent and gateway | still to be added | PRD 0086 (design repo) |

## Trust domains and tokens

`contract/auth.py`, `contract/hostctl.py`

Callers present compact HS256 JWTs whose payload carries a **role**.

| Domain | Key holder | Roles | Key env | Token env |
|---|---|---|---|---|
| `control-plane` (`CONTROL_PLANE`) | orchestrator, host CLI | `cli`, `gateway` | `BOT_BOTTLE_ORCHESTRATOR_TOKEN` | `BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT` |
| `host-control` (`HOST_CONTROL`) | host controller, host CLI | `cli`, `orchestrator`, `gateway` | `BOT_BOTTLE_HOSTCTL_KEY` | `BOT_BOTTLE_HOSTCTL_JWT` |

A token is signed, not encrypted, and long-lived. The protection is that only
the key holder can mint one.

- The orchestrator never holds the `host-control` key. It gets an
  `orchestrator`-role token, so it can't mint itself a `cli` token.
- The gateway gets a `gateway` token for each domain it calls, never a key.

## Orchestrator control plane

`contract/http.py`, served by `orchestrator/api.py`

| Rule | Detail |
|---|---|
| **Auth** | The token goes in `x-bot-bottle-orchestrator-auth` (`ORCHESTRATOR_AUTH_HEADER`). Missing or invalid gets 401. A valid token with the wrong role gets 403. |
| **Limits** | Request bodies are at most 1 MiB (`MAX_BODY_BYTES`) and must arrive within 10 s (`REQUEST_BODY_TIMEOUT_SECONDS`). Unknown body fields are ignored. |
| **Roles** | Every route is `cli`-only except `GET /health` (no auth) and the three gateway routes below. |

| Route | Role | Does |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /gateway` | `cli` | the gateway's status |
| `GET /bottles` | `cli` | the registry (secrets redacted) |
| `POST /bottles` | `cli` | register a bottle: `source_ip`, `metadata`, `policy`, `tokens`, `env_var_secret`. Returns `bottle_id`, `identity_token`. |
| `PUT /bottles/{id}/policy` | `cli` | replace a bottle's egress policy |
| `POST /bottles/{id}/reprovision_gateway` | `cli` | re-push a bottle's gateway state |
| `POST /bottles/{id}/secret` | `cli` | update one egress secret |
| `DELETE /bottles/{id}` | `cli` | deregister |
| `POST /reconcile` | `cli` | reap rows for bottles that aren't running (`grace_seconds`, default 120). 503 if the live set is unknown. |
| `POST /attribute` | `cli` | the `bottle_id` for a `source_ip` and `identity_token`. 403 if neither matches. |
| `GET /supervise/proposals` | `cli` | pending supervise proposals |
| `POST /supervise/respond` | `cli` | approve or deny a proposal. 409 `already answered` if it has a decision. |
| `POST /resolve` | `gateway`, `cli` | resolve a source IP and identity token to a bottle's policy and tokens |
| `POST /supervise/propose` | `gateway`, `cli` | file a proposal |
| `POST /supervise/poll` | `gateway`, `cli` | wait for a decision |
| `POST /launches` | `cli` | relay a launch to the host controller. Returns `{"job"}`. |
| `GET /launches/{job}` | `cli` | the launch job's state |
| `GET /launches/{job}/events` | `cli` | the job's events (NDJSON stream) |
| `GET /bottles/{id}/attach` | `cli` | WebSocket relay of the bottle's terminal |

### Launches

`POST /launches` takes `agent`, `bottles`, `label`, `color`, `prompt`,
`headless`, `image_policy` and `assume_yes`.

- Paths and secret values are never forwarded.
- Without `assume_yes`, the job ends with `{"type": "error", "code":
  "confirm"}`.
- The `/launches` and attach routes answer 503 when the orchestrator has no
  host controller.

### Process inputs

```sh
python -m bot_bottle.orchestrator --host H --port P --db PATH
```

The signing key goes in `BOT_BOTTLE_ORCHESTRATOR_TOKEN`. The orchestrator
refuses to start without one. `BOT_BOTTLE_LOG_LEVEL=debug` turns on the
access log.

## Host controller (as the orchestrator sees it)

`contract/hostctl.py`, served by `control/hostctl/server.py`

The host controller is the per-host daemon behind every bottle operation. An
orchestrator it brought up reaches it through these.

| Input | Detail |
|---|---|
| **Provisioning** | `BOT_BOTTLE_HOSTCTL_URL` (`HOSTCTL_URL_ENV`) and an `orchestrator` token in `BOT_BOTTLE_HOSTCTL_JWT`. An orchestrator started any other way gets neither. |
| **Address** | Port 8098 (`GUEST_PORT`) on the host's end of the orchestrator's network: the docker publish bridge, the macOS vmnet gateway or the firecracker orchestrator TAP. Never `0.0.0.0`. |
| **Listener** | It accepts only peers on that network, and only the `orchestrator` and `gateway` roles. It answers `/bottles/live` only for its own backend. |
| **Auth** | `Authorization: Bearer <token>`. |

| Route | Roles | Does |
|---|---|---|
| `GET /status` | none | build id and pid |
| `POST /bottles` | `cli`, `orchestrator` | start a launch job (a `LaunchRequest`). Returns `{"job"}`. `cwd`, `copy_cwd`, `secrets` and `host_keys` are `cli`-only. |
| `GET /bottles` | `cli`, `orchestrator` | bottles this daemon holds |
| `DELETE /bottles/{id}` | `cli`, `orchestrator` | start a teardown job |
| `GET /bottles/live` | `cli`, `orchestrator` | `{"bottles": [{backend, slug, source_ip}], "errors": {backend: message}}`, read from the backends |
| `GET /sessions/{id}` | `cli`, `orchestrator` | whether the agent is running, and its exit code |
| `GET /sessions/{id}/attach` | `cli`, `orchestrator` | WebSocket. Terminal bytes travel as binary frames. `resize`, `writable`, `exit` and `detached` travel as JSON text frames. Viewers are read-only unless the agent's manifest allows writing. |
| `GET /jobs`, `GET /jobs/{id}` | the job's owner | job list and state |
| `GET /jobs/{id}/events` | the job's owner | NDJSON: `log` lines, then one `result` or `error` |

A backend in `errors` means its list is unknown, not empty. Callers must not
treat it as "nothing is running".

## Internal environment

These are the `BOT_BOTTLE_*` variables bot-bottle sets for its own processes.
None is a knob. The ones you can set are in [install.md](install.md#settings).

The table is generated from `bot_bottle/env_vars.py` by
`scripts/gen_env_docs.py`.

<!-- env-vars:internal:begin -->
| Variable | Does | Default |
|---|---|---|
| `BOT_BOTTLE_ORCHESTRATOR_URL` | The orchestrator's control-plane URL, for the gateway's daemons. | — |
| `BOT_BOTTLE_ORCHESTRATOR_TOKEN` | The `control-plane` signing key; the orchestrator refuses to start without it. | — |
| `BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT` | The gateway's `gateway`-role token for the control plane. | — |
| `BOT_BOTTLE_HOSTCTL_URL` | The host controller's URL, for an orchestrator it brought up. | — |
| `BOT_BOTTLE_HOSTCTL_KEY` | The `host-control` signing key. | — |
| `BOT_BOTTLE_HOSTCTL_JWT` | A `host-control` token: `orchestrator`-role for the orchestrator, `gateway`-role for the gateway. | — |
| `BOT_BOTTLE_GATEWAY_DAEMONS` | Which gateway daemons run (`egress`, `git-http`, `supervise`). | all |
| `BOT_BOTTLE_GIT_GATE_READY_FILE` | When set, `git-http` waits for this file before it starts. | — |
| `BOT_BOTTLE_SENSITIVE_PREFIXES` | Comma-separated env var name prefixes whose values egress DLP blocks as known secrets (the canary). | — |
| `BOT_BOTTLE_SOURCE_HASH` | macos-container: the source hash the running orchestrator was built from, to spot a stale one. | — |
| `BOT_BOTTLE_CLAUDE_HOST_ACCESS_TOKEN` | Secret name the gateway injects a forwarded Claude login under. | — |
| `BOT_BOTTLE_CODEX_HOST_ACCESS_TOKEN` | Secret name the gateway injects a forwarded Codex login under. | — |
| `BOT_BOTTLE_VERSION` | The host bot-bottle version string, injected into infra containers at startup so `/version` can report it. | — |
| `BOT_BOTTLE_COMMIT` | The host bot-bottle git commit hash, injected into infra containers at startup so `/version` can report it. | — |
| `BOT_BOTTLE_PROTOCOL` | The host bot-bottle protocol version (integer), injected into infra containers at startup so `/version` can report it. | — |
<!-- env-vars:internal:end -->

## Changelog

- **2026-10-22:** first version. Control-plane routes and roles, the host
  controller's API as an orchestrator sees it, both trust domains, and the env
  each service reads (PRD 0087 chunk 7, design repo).
- **2026-10-30:** the internal environment table, generated from
  `bot_bottle/env_vars.py`. `BOT_BOTTLE_ORCHESTRATOR_DEBUG` is gone. Use
  `BOT_BOTTLE_LOG_LEVEL=debug`.
