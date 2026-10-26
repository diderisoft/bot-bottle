# Architecture

How bot-bottle is put together today. This page and the others in
`docs/reference/` describe the current system and are edited in place. PRDs
record why it got this way; when a PRD ships, it updates the reference page it
changes.

## Components

Each host runs one **host controller**, one **orchestrator** and one
**gateway**. Each bottle adds one **agent runtime**.

| Component | Runs as | Owns | Code |
|---|---|---|---|
| CLI | the user, on the host | prompts, pickers, secrets from the env; talks only to the host controller | `cli/` |
| Host controller | per-host daemon, as the user | launch, teardown, listing, tmux sessions, attach, the audit journal, the firehose, remote access | `control/hostctl/` |
| Backend | library inside the host controller | building images and starting the agent and infra in containers or microVMs | `backend/` |
| Orchestrator | per-host service in a container or microVM | the bottle registry, egress tokens (encrypted at rest), the supervise queue, the control-plane signing key; sole opener of `bot-bottle.db` | `orchestrator/` |
| Gateway | per-host service in a container or microVM | the egress proxy, git-gate, the supervise MCP server, the mitmproxy CA | `gateway/` |
| Agent runtime | one container or microVM per bottle | the model harness (claude, codex, pi) and the workspace | `contrib/<provider>/` |

The orchestrator and gateway are the **infra pair**. They start on first
launch, are adopted if already healthy, and keep running between bottles. See
[backends.md](backends.md) for how each backend builds them.

```
  CLI ──► host controller ──► backend
              │  ▲                │ starts
              │  │ /bottles/live  ▼
              │  └──────── orchestrator ◄──── gateway ◄──── agent
              │   audit,      registry,      egress,       proxy URLs
              │   firehose    tokens,        git-gate,     only
              └──────────────► queue         supervise
```

Agents reach only the gateway. The gateway reaches the orchestrator over a
separate control network. The orchestrator reaches the host controller on
port 8098 of its own network, never `0.0.0.0`.

## Layers

`bot_bottle/` has three layers, enforced by `tests/unit/test_layering.py`
([PRD 0087](../prds/0087-host-service-boundary.md)):

| Layer | Packages | May import |
|---|---|---|
| contract | `contract/` | stdlib, `contract` |
| lib | `lib/` | stdlib, `lib` |
| services | `orchestrator/`, `gateway/` | stdlib, their pinned deps, `contract`, `lib`, themselves |
| host | everything else | anything |

The service images copy only `contract/`, `lib/` and their own package, so a
CLI change doesn't rebuild them (`image_inputs.py` hashes the same paths).
`contract/` is the wire format between host and services; it is specified in
[runtime-contract.md](runtime-contract.md).

## Code map

| Package / module | Does |
|---|---|
| `manifest/` | loads agent and bottle Markdown files, resolves `extends:`, seals the bottle, project bottle and agent trust |
| `agent_provider.py`, `contrib/` | per-harness image, command, auth and egress routes |
| `egress/`, `git_gate/`, `supervisor/` | host-side plans for each gateway service, built from the sealed bottle |
| `backend/base.py` | `BottleBackend`: `prepare` → `launch` → `provision` → teardown |
| `backend/launch_common.py`, `provision_gateway.py`, `gateway_attach.py` | launch steps every backend shares |
| `backend/{docker,firecracker,macos_container}/` | per-backend launch, infra, cleanup, freeze |
| `control/` | host side of the control plane: orchestrator client and lifecycle, registration, git-gate render, CA rotation |
| `control/hostctl/` | the host controller daemon |
| `contract/` | routes, token roles, shared constants, the YAML subset parser |
| `orchestrator/` | FastAPI control plane and its SQLite stores |
| `gateway/` | mitmproxy addon, git smart-HTTP server, supervise server, bootstrap |
| `lib/` | stdlib-only helpers shared by host and services |

## Launch

1. **CLI** resolves the agent, bottles, label and secrets, then sends a
   `LaunchRequest` to the host controller.
2. **Host controller** starts a launch job. The backend `prepare`s a plan:
   validates the manifest and builds the egress, git-gate, supervise, env and
   workspace plans.
3. **Backend** builds or loads the agent image and ensures the infra pair is
   up.
4. **Registration.** The launcher mints any git-gate deploy keys, registers
   the bottle with the orchestrator (source IP, policy, egress tokens) and
   gets back a `bottle_id` and an identity token. It provisions the bottle's
   git-gate repos and credentials into the gateway.
5. **Agent start.** The agent starts on its pinned source IP with
   `HTTP(S)_PROXY=http://<bottle_id>:<identity token>@<gateway>`, the gateway
   CA, and git-gate and supervise URLs. Real tokens stay in the orchestrator.
6. **Isolation probe.** From inside the agent, the launcher checks that the
   host and a public address are unreachable without the gateway, and refuses
   to continue if either is reachable. See [backends.md](backends.md).
7. **Session.** The agent runs in a tmux session on bot-bottle's own tmux
   server. The CLI attaches; Ctrl-] detaches and leaves it running.
8. **Teardown** on agent exit: deregister from the orchestrator, revoke
   deploy keys, remove the container or VM. The infra pair keeps running.

## Attribution

One gateway serves every bottle, so each request must be tied to a bottle.
The key is the pair **(source IP, identity token)**:

- The backend pins the agent's source IP, and the agent can't change it.
- The identity token is the password in the agent's proxy URL
  (`Proxy-Authorization: Basic`), or the `x-bot-bottle-identity` header for
  git-gate.

Each gateway service resolves the pair through the orchestrator's
`POST /resolve` on every request, with no cache. An unknown pair or an
unreachable orchestrator fails closed. The orchestrator's `reconcile` loop
removes rows for bottles that are no longer running, so a recycled IP can't
inherit an old bottle's policy.

## Trust

Two signing domains keep services from minting each other's credentials
([PRD 0079](../prds/0079-control-plane-auth-provisioning.md)):

- `control-plane`: the orchestrator holds the key and mints `cli` and
  `gateway` tokens.
- `host-control`: the host controller holds the key. The orchestrator gets an
  `orchestrator` token and never the key, so it can't forge a `cli` token for
  the host controller that starts and stops it.

Roles, routes and env vars are in [runtime-contract.md](runtime-contract.md).

## State

| What | Where | Owner |
|---|---|---|
| User agents and bottles | `~/.bot-bottle/{agents,bottles}/`, versioned in a git repo in `~/.bot-bottle/` that tracks only those two directories (`manifest/versions.py`, [PRD 0103](../prds/0103-forge-connect-and-bottle-onboarding.md)). bot-bottle commits its own writes, and hand edits as "edited by hand" before a launch reads them; it refuses a commit holding a value from the secrets file. The launch audit event records the version (`bottle_versions`), and `bot-bottle bottle history` / `revert` read and restore them | user |
| Project agents and bottles | `<repo>/.bot-bottle/` (bottles, and agents whose `agent_provider` grants anything, need `bot-bottle trust`). A project bottle is named `./<name>`, so it never shadows or is shadowed by a host bottle; bare `bot-bottle` uses `./main` when the project has one | repo |
| Registry, supervise queue, encrypted egress secrets, settings | `bot-bottle.db`: `~/.bot-bottle/db/` mounted into the orchestrator; on firecracker, the orchestrator VM's `/dev/vdb` volume | orchestrator |
| Signing keys | `~/.bot-bottle/<key file>`, mode 0600, pushed to the key holder | host |
| Gateway CA | `~/.bot-bottle/gateway-ca/`, mounted at mitmproxy's confdir (firecracker: the infra VM dir's `gateway-ca/`) | gateway |
| Audit journal | `~/.bot-bottle/audit/` | host controller |
| Per-bottle metadata, compose files, logs | `~/.bot-bottle/state/<identity>/` | backend |
| Image and kernel caches | `~/.cache/bot-bottle/`, and the runtime's own image store; `bot-bottle prune` removes superseded releases at once and unused builds after 14 days, never a current pin or an image in use ([PRD 0088](../prds/0088-prune-cached-agent-images.md)) | backend |

The gateway keeps no durable per-bottle state. When it restarts, the launcher
re-provisions every running bottle: CA, git-gate repos and credentials, and
egress tokens ([PRD 0081](../prds/0081-reprovision-gateway-state-on-bringup.md)).

## Other reference pages

- [backends.md](backends.md): the three backends and their isolation
- [gateway.md](gateway.md): egress proxy, DLP, supervise, firehose, CA
- [git-gate.md](git-gate.md): the git push and fetch gate
- [host-controller.md](host-controller.md): sessions, jobs, audit, remote access
- [runtime-contract.md](runtime-contract.md): routes, roles, tokens and env
- [glossary.md](glossary.md): terms
- [ci.md](ci.md), [image-build-inputs.md](image-build-inputs.md): CI and image hashing
