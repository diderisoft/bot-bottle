# Architecture

How bot-bottle is put together today. Pages in `docs/reference/` describe the
current system and are edited in place, so a change updates the pages it
affects.

Why it got this way is in [`docs/decisions/`](../decisions/README.md). PRDs
mentioned by number live in the private design repo (ADR 0010). Nothing here
depends on them.

## How the pieces fit

The CLI asks a per-host host controller to launch a bottle. Each bottle is one
agent runtime, and its traffic leaves only through a shared gateway.

```
  CLI ──► host controller ──► backend
              │  ▲                │ starts
              │  │ /bottles/live  ▼
              │  └──────── orchestrator ◄──── gateway ◄──── agent
              │   audit,      registry,      egress,       proxy URLs
              │   firehose    tokens,        git-gate,     only
              └──────────────► queue         supervise
```

- Agents reach only the gateway.
- The gateway reaches the orchestrator over a separate control network.
- The orchestrator reaches the host controller on port 8098 of its own
  network, never `0.0.0.0`.

## Components

Each host runs one **host controller**, one **orchestrator** and one
**gateway**. Each bottle adds one **agent runtime**.

| Component | Runs as | Owns | Code |
|---|---|---|---|
| CLI | the user, on the host | prompts, pickers, secrets from the env. It talks only to the host controller | `cli/` |
| Host controller | per-host daemon, as the user | launch, teardown, listing, tmux sessions, attach, the audit journal, the firehose, remote access | `control/hostctl/` |
| Backend | library inside the host controller | building images and starting the agent and infra in containers or microVMs | `backend/` |
| Orchestrator | per-host service in a container or microVM | the bottle registry, egress tokens (encrypted at rest), the supervise queue, the control-plane signing key. It is the sole opener of `bot-bottle.db` | `orchestrator/` |
| Gateway | per-host service in a container or microVM | the egress proxy, git-gate, the supervise MCP server, the mitmproxy CA | `gateway/` |
| Agent runtime | one container or microVM per bottle | the model harness (claude, codex, pi) and the workspace | `contrib/<provider>/` |

The orchestrator and gateway are the **infra pair**. They start on first
launch, are adopted if already healthy, and keep running between bottles.
[backends.md](backends.md) says how each backend builds them.

### One orchestrator per environment

An environment ([install.md](install.md#environments)) runs exactly one
orchestrator. Each bring-up records which backend's orchestrator it is, and
its host URL, in `<state root>/infra/orchestrator.json`.

A bring-up is `bot-bottle up` or a launch's implicit one
(`InfraService.ensure_running`). Everything that talks to "the orchestrator"
uses that record and never probes another backend's:

- credential refresh
- supervise requests and answers
- the console relay
- the running manifest view
- `status` and `doctor`

With none recorded, or the recorded one down, they say so and point at
`bot-bottle up`.

Bringing up a second backend's pair in the same environment is refused while
the recorded one still answers. Stop the first (`bot-bottle down --backend
<it>`) or use another environment.

`status` and `doctor` warn about other bot-bottle orchestrators answering at a
backend's well-known address, such as a leftover test run's. They say how to
remove them (`control/client.py`).

### From the agent's side

Every backend builds the same shape. Each agent session runs in tmux, so it
outlives the terminal that started it and can have several viewers
([host-controller.md](host-controller.md#sessions)).

```
  host CLI ──► host controller ──► backend (launch / teardown)
                                       │
  ┌──────────── per host ──────────────┼──────────────────────────────────┐
  │                                    ▼                                  │
  │  ┌───────────────┐       ┌─────────────────────────┐                  │
  │  │ agent         │ HTTPS │ gateway                 │ HTTPS to         │
  │  │ (claude,      │──────►│  egress proxy (DLP,     │──► allowlisted   │
  │  │  codex, pi)   │       │   token injection)      │    hosts         │
  │  │               │  git  │  git-gate (gitleaks)    │ SSH push to      │
  │  │ env: proxy    │──────►│  supervise              │──► upstreams     │
  │  │ URLs only,    │       └────────────┬────────────┘                  │
  │  │ no real       │                    │ control network               │
  │  │ tokens        │       ┌────────────▼────────────┐                  │
  │  └───────────────┘       │ orchestrator            │                  │
  │   one per bottle;        │  registry, signing key, │                  │
  │   no route to the        │  approval queue         │                  │
  │   control plane          └─────────────────────────┘                  │
  └───────────────────────────────────────────────────────────────────────┘
```

Each backend confines the agent to that picture in its own way: a host-only
network plus a pf anchor, a TAP plus an nft table, or an `--internal` network.
The comparison is in [backends.md](backends.md#comparison).

## Launch

A launch goes from the CLI through the host controller to a running,
probed agent.

1. **CLI** resolves the agent, bottles, label and secrets. It sends a
   `LaunchRequest` to the host controller.
2. **Host controller** starts a launch job. The backend `prepare`s a plan: it
   validates the manifest and builds the egress, git-gate, supervise, env and
   workspace plans.
3. **Backend** builds or loads the agent image and ensures the infra pair is
   up.
4. **Registration.** The launcher mints any git-gate deploy keys. It registers
   the bottle with the orchestrator (source IP, policy, egress tokens) and gets
   back a `bottle_id` and an identity token.
5. **Gateway provisioning.** The launcher provisions the bottle's git-gate
   repos and credentials into the gateway.
6. **Agent start.** The agent starts on its pinned source IP with
   `HTTP(S)_PROXY=http://<bottle_id>:<identity token>@<gateway>`, the gateway
   CA, and git-gate and supervise URLs. Real tokens stay in the orchestrator.
7. **Isolation probe.** From inside the agent, the launcher checks that the
   host and a public address are unreachable without the gateway. It refuses
   to continue if either is reachable. See [backends.md](backends.md).
8. **Session.** The agent runs in a tmux session on bot-bottle's own tmux
   server. The CLI attaches. Ctrl-] detaches and leaves it running.
9. **Teardown** on agent exit: deregister from the orchestrator, revoke
   deploy keys, remove the container or VM. The infra pair keeps running.

## Attribution

Each request is tied to a bottle by the pair **(source IP, identity token)**.
One gateway serves every bottle, so it needs that pair.

- The backend pins the agent's source IP, and the agent can't change it. On
  macos-container, an agent that can get root can
  ([backends](backends.md#agents-cant-reach-each-other)).
- The identity token is the password in the agent's proxy URL
  (`Proxy-Authorization: Basic`). For git-gate it is the
  `x-bot-bottle-identity` header.

Each gateway service resolves the pair through the orchestrator's
`POST /resolve` on every request, with no cache. An unknown pair or an
unreachable orchestrator fails closed.

The orchestrator's `reconcile` loop removes rows for bottles that are no
longer running. So a recycled IP can't inherit an old bottle's policy.

## Trust

Two signing domains keep services from minting each other's credentials
(PRD 0079, design repo).

| Domain | Key holder | Mints |
|---|---|---|
| `control-plane` | the orchestrator | `cli` and `gateway` tokens |
| `host-control` | the host controller | an `orchestrator` token for the orchestrator |

The orchestrator never gets the `host-control` key. So it can't forge a `cli`
token for the host controller that starts and stops it.

Roles, routes and env vars are in [runtime-contract.md](runtime-contract.md).

## State

Where each kind of state lives, and what owns it.

| What | Where | Owner |
|---|---|---|
| User agents and bottles | `~/.bot-bottle/{agents,bottles}/`, versioned ([below](#versioned-user-files)) | user |
| Project agents and bottles | `<repo>/.bot-bottle/`, run only once trusted. Trust is kept in `~/.bot-bottle/trusted-bottles.json` ([below](#project-files-and-trust)) | repo |
| Forge connections | `~/.bot-bottle/forges.md` ([below](#forge-connections)) | host |
| Registry, supervise queue, encrypted egress secrets, settings | `bot-bottle.db`: `~/.bot-bottle/db/` mounted into the orchestrator. On firecracker, the orchestrator VM's `/dev/vdb` volume | orchestrator |
| The environment's orchestrator (backend and host URL) | `~/.bot-bottle/infra/orchestrator.json`, written by each bring-up | host controller |
| Signing keys | `~/.bot-bottle/<key file>`, mode 0600, pushed to the key holder | host |
| Gateway CA | `~/.bot-bottle/gateway-ca/`, mounted at mitmproxy's confdir (firecracker: the infra VM dir's `gateway-ca/`) | gateway |
| Audit journal | `~/.bot-bottle/audit/` | host controller |
| Per-bottle metadata, compose files, logs | `~/.bot-bottle/state/<identity>/` ([below](#per-bottle-metadata)) | backend |
| Image and kernel caches | `~/.cache/bot-bottle/`, and the runtime's own image store ([below](#image-caches)) | backend |

Paths are the live environment's. A named environment, picked by `BOT_BOTTLE_ENV`, keeps
the same layout under `~/.bot-bottle-envs/<name>/`. It prefixes its
containers, VMs, networks and volumes `bb-<name>-`, and shares the caches
([install.md](install.md#environments)).

The gateway keeps no durable per-bottle state. When it restarts, the launcher
re-provisions every running bottle: CA, git-gate repos and credentials, and
egress tokens (PRD 0081, design repo).

### Versioned user files

`~/.bot-bottle/` holds a git repo that tracks only `agents/` and `bottles/`
(`manifest/versions.py`, PRD 0103 in the design repo).

- bot-bottle commits its own writes.
- Hand edits are committed as "edited by hand" before a launch reads them.
- It refuses a commit holding a value from the secrets file.
- The launch audit event records the version (`bottle_versions`).
- `bot-bottle bottles history` and `revert` read and restore versions.
- `bottles migrate` (`manifest/migrate.py`) rewrites an old-format bottle as
  forge grants, as a version.

### Project files and trust

Project bottles, and some project agents, run only once `bot-bottle trust`
records them in `~/.bot-bottle/trusted-bottles.json`.

- Every project bottle needs trust.
- A project agent needs trust when its `agent_provider` grants anything, or
  when it runs on a host bottle granting more than `basic`.
- A project bottle's trust also covers this host's connection to each forge
  its repos are on (ADR 0019).

A project agent's trust hash (`manifest/trust.py`) covers three things:

- its `.md`
- what it gets from the host
- every file its image build takes from the launch directory

The build files are a custom `agent_provider.dockerfile`'s bytes, and an
overlay's staged build context. That context is its Dockerfile plus that
Dockerfile's whole directory, or its `files`.

`agent_overlay.inputs_digest` digests the overlay context. It is the overlay
cache key's context, staged on a placeholder base, so the two can't drift.

Editing any of these files asks again. So does a missing input appearing.

Trust records from before builds were covered have no `scheme`. They
re-prompt once for agents that build, and the prompt says why.

A project agent replaces a home agent of the same name, and its trust prompt
says so.

A project bottle is named `./<name>`, so it never shadows or is shadowed by a
host bottle. `bot-bottle <agent>` uses `./main` when the project has one.

### Onboarding

A terminal launch in a project with no project bottle first drafts one from
the checkout (`onboarding.py`). Once you accept it, bot-bottle writes it and
trusts it.

The draft grants the project's `origin`, read-only, with the forge's
description, when this host connected that forge. When it hasn't, onboarding
offers `forge connect`.

### Forge connections

`~/.bot-bottle/forges.md` lists each forge this host connected, keyed by host.
Only `bot-bottle forge connect` (`forge_connections.py`) writes it.

Each entry has the forge's URL, SSH address, pinned host key and users. A user
entry holds its token secret name, the default flag, and GitHub's `app` and
`transport`.

Grants resolve through this file, never through a bottle's description of the
forge ([ADR 0019](../decisions/0019-forges-are-described-in-bottles-connected-on-hosts.md)).

### Per-bottle metadata

`~/.bot-bottle/state/<identity>/metadata.json` holds these fields:

`identity`, `agent_name`, `cwd`, `copy_cwd`, `started_at`, `compose_project`,
`backend`, `label`, `color`, `bottle_names`, `origin`, `agent_id`, `parent`.

`parent` is the parent bottle's identity when launched via `launch-bottle`
(PRD 0106), and empty otherwise.

### Image caches

`bot-bottle cleanup images` removes superseded releases at once, and unused
builds after 14 days. It never removes a current pin or an image in use (PRD
0088, design repo).

## Other reference pages

- [backends.md](backends.md): the three backends and their isolation
- [install.md](install.md): installing, host requirements, environments, uninstalling
- [manifest.md](manifest.md): bottle and agent files, trust, egress routes
- [gateway.md](gateway.md): egress proxy, DLP, supervise, firehose, CA
- [git-gate.md](git-gate.md): the git push and fetch gate
- [host-controller.md](host-controller.md): sessions, jobs, audit, remote access
- [runtime-contract.md](runtime-contract.md): routes, roles, tokens and env
- [glossary.md](glossary.md): terms
- [ci.md](ci.md), [image-build-inputs.md](image-build-inputs.md): CI and image hashing

## For contributors

### Layers

`bot_bottle/` has three layers, enforced by `tests/unit/test_layering.py`
(PRD 0087, design repo).

| Layer | Packages | May import |
|---|---|---|
| contract | `contract/` | stdlib, `contract` |
| lib | `lib/` | stdlib, `lib` |
| services | `orchestrator/`, `gateway/` | stdlib, their pinned deps, `contract`, `lib`, themselves |
| host | everything else | anything |

The service images copy only `contract/`, `lib/` and their own package. So a
CLI change doesn't rebuild them. `image_inputs.py` hashes the same paths.

`contract/` is the wire format between host and services. It is specified in
[runtime-contract.md](runtime-contract.md).

### Code map

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
