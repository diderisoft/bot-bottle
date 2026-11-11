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
| Host controller | per-host daemon, as the user | launch, teardown, listing, tmux sessions, attach, the audit journal, the firehose, remote access, the host database `bot-bottle.db` | `control/hostctl/` |
| Backend | library inside the host controller | building images and starting the agent and infra in containers or microVMs | `backend/` |
| Orchestrator | per-host service in a container or microVM | the bottle registry, egress tokens (encrypted at rest), the supervise queue, the control-plane signing key. It is the only opener of its own database, `registry.db` | `orchestrator/` |
| Gateway | per-host service in a container or microVM | the egress proxy, git-gate, the supervise MCP server, the mitmproxy CA | `gateway/` |
| Agent runtime | one container or microVM per bottle | the model harness (claude, codex, pi) and the workspace | `contrib/<provider>/` |

The orchestrator and gateway are the **infra pair**. They start on first
launch, are adopted if already healthy, and keep running between bottles.
[backends.md](backends.md) says how each backend builds them.

### One orchestrator per environment

An environment ([install.md](install.md#environments)) runs exactly one
orchestrator. Each bring-up records which backend's orchestrator it is, and
its host URL, in the host database (`infra_state`).

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
| Project agents and bottles | `<repo>/.bot-bottle/`, run only once trusted. Trust is kept in the host database ([below](#project-files-and-trust)) | repo |
| Host settings, forge connections, bottled agents and their sessions, audit forwarding cursors | `~/.bot-bottle/db/bot-bottle.db` ([below](#databases), [forge connections](#forge-connections), [bottled agents](#bottled-agents)) | host controller |
| The audit index | `~/.bot-bottle/db/index.db`, rebuilt from the journal ([below](#databases)) | host controller |
| Registry, supervise queue, encrypted egress secrets, canary events | `registry.db` ([below](#databases)) | orchestrator |
| What infra bring-up recorded: the environment's orchestrator (backend and host URL), each backend's infra settings, docker's subnet and ports | `bot-bottle.db`'s `infra_state`, written by each bring-up | host controller |
| Signing keys | `~/.bot-bottle/<key file>`, mode 0600, pushed to the key holder | host |
| Gateway CA | `~/.bot-bottle/gateway-ca/`, mounted at mitmproxy's confdir (firecracker: `~/.bot-bottle/infra/gateway-ca/`, pushed into the gateway VM) | gateway |
| Audit journal | `~/.bot-bottle/audit/` | host controller |
| Per-bottle scratch: compose files, env and prompt files, logs, the preserve marker, git-gate's re-provisioning state ([git-gate.md](git-gate.md#re-provisioning-state)) | `~/.bot-bottle/state/<identity>/` | backend |
| Image and kernel caches, VM run dirs, firecracker's build store | `~/.cache/bot-bottle/`, and the runtime's own image store ([below](#image-caches)). Only what can be downloaded or rebuilt | backend |

Paths are the live environment's. A named environment, picked by `BOT_BOTTLE_ENV`, keeps
the same layout under `~/.bot-bottle-envs/<name>/`. It prefixes its
containers, VMs, networks and volumes `bb-<name>-`, and shares the caches
([install.md](install.md#environments)).

The gateway keeps no durable per-bottle state. When it restarts, the launcher
re-provisions every running bottle: CA, git-gate repos and credentials, and
egress tokens (PRD 0081, design repo).

### Databases

Each database has one job and one writer
([ADR 0020](../decisions/0020-where-host-state-lives.md)).

| Database | Holds | Writer | Where |
|---|---|---|---|
| `bot-bottle.db` | the host's own state: settings (`bot_bottle_config`), forge connections (`forge_connections`, `forge_users`), trust records (`trust_records`), the console pairing and its link's state (`pairing`, `console_link`), trusted audit passkeys (`audit_passkeys`), bottled agents (`bottled_agents`) and their tmux sessions (`sessions`), each one's manifest versions and what it launched with (`run_manifest_versions`, `run_manifest_launches`), the remembered agent (`agent_prefs`), transcript copies' offsets (`transcript_runs`, `transcript_files`), snapshots (`snapshots`), isolation passes (`isolation_passes`), what infra bring-up recorded (`infra_state`), Firecracker workspace disks' owners (`workspace_disks`), the audit forwarder's and console push's cursors (`audit_push_cursors`, `audit_push_runs`) | host controller | `~/.bot-bottle/db/` |
| `index.db` | indexes derived from logs: the audit index (`audit_events`, `audit_index_cursor`) | host controller | `~/.bot-bottle/db/` |
| `registry.db` | the orchestrator's working state: the bottle registry, encrypted egress secrets, the supervise queue and its answers, canary events | orchestrator | docker: `~/.bot-bottle/registry/` (0700), the orchestrator's only mount. macOS: a container volume. Firecracker: the orchestrator VM's `/dev/vdb` volume, `~/.bot-bottle/infra/registry.ext4` |

- One helper opens every connection (`lib/db_store.connect`), in WAL mode
  with a 5 s busy timeout.
- Each writer migrates its database when it starts, without asking. It drops
  any table none of its stores' `TABLES` names, such as what an older version
  kept there, and then VACUUMs.
- A store that reads rows and writes from them does both in one
  `BEGIN IMMEDIATE` transaction (`db_store.immediate`).
- The CLI reads and changes host state through the host controller's API
  ([host-controller.md](host-controller.md#host-database)). Only
  `bot-bottle uninstall` and `host restore-db` touch the file itself.
- The host controller checks `bot-bottle.db` at start and keeps a daily
  backup ([checks and backups](host-controller.md#checks-and-backups)).
- `index.db` has no backup. A missing or damaged one is deleted and rebuilt
  from its log at start. A new derived index goes here, never in
  `bot-bottle.db` (`index_db.py`).
- Each store marks its tables portable or machine-specific (`TABLES`). The
  host settings, forge connections and audit passkeys are portable, except a
  connection's `ssh`, this machine's address for the forge. Everything else
  is machine-specific. Nothing reads the mark yet.
- No secret value is in `bot-bottle.db` or `index.db`. A row names a secret
  in the host secrets file (`secret:NAME`), as forge users and the pairing's
  console tokens do.

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
records them in the host database (`trust_records`). The host controller
writes them, and the CLI asks it.

- Every project bottle needs trust.
- A project agent needs trust when its `agent_provider` grants anything, or
  when it runs on a host bottle granting more than `basic`.
- A project bottle's trust also covers this host's connection to each forge
  its repos are on (ADR 0019).

A project agent's trust hash (`manifest/trust.py`) covers three things:

- its `.md`
- what it gets from the host
- every file its image build takes from the launch directory

The build files are a custom `agent_provider.dockerfile_path`'s bytes, and an
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

`bot-bottle.db` holds each forge this host connected, keyed by host
(`forge_connections.py`). `bot-bottle forge list` prints them.

| Table | Row |
|---|---|
| `forge_connections` | a forge's kind, URL, SSH address and pinned host key |
| `forge_users` | a user on it: its token's secret name (`secret:NAME`), the default flag, and GitHub's `app` and `transport` |

- Only `forge connect`, the console's connect and `forge remove` change them,
  through the host controller.
- Grants resolve through them, never through a bottle's description of the
  forge ([ADR 0019](../decisions/0019-forges-are-described-in-bottles-connected-on-hosts.md)).

### Bottled agents

A bottled agent is a row of `bot-bottle.db`'s `bottled_agents`, with these
fields:

`identity`, `agent_name`, `cwd`, `copy_cwd`, `started_at`, `compose_project`,
`backend`, `label`, `color`, `bottle_names`, `origin`, `agent_id`, `parent`.

- The label is kept only here. Its session record points at the row by
  identity, so a rename changes one place.
- `parent` is the parent bottle's identity when launched via `launch-bottle`
  (PRD 0106), and empty otherwise.
- Once it runs, the row also holds how a restarted host controller finishes
  it ([host-controller.md](host-controller.md#after-a-daemon-restart)).
- Removing a bottled agent deletes its state dir, its snapshots and its rows
  (`bottled_agents`, `sessions`, `snapshots`) in one call to the host
  controller. Listings read the rows, never the directories.
- `cleanup` removes a bottled agent whose bottle isn't live, unless it's kept
  for resume. It also removes one that records no backend, and a state dir
  no row records, which nothing can resume.

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
