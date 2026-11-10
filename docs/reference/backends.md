# Backends

You get macos-container on a Mac with Apple's `container` CLI, firecracker on
a KVM-capable Linux host, and docker only when you ask for it or confirm a
prompt.

macos-container and firecracker run each agent in its own VM. Docker is the
weakest boundary: agents share the host kernel unless gVisor is registered.

A backend runs the agent and the infra pair (orchestrator and gateway) with one
isolation primitive. The backends differ in how the agent runs, how it gets a
pinned address, and what stops it from leaving the box except through the
gateway.

## Comparison

| | macos-container | firecracker | docker |
|---|---|---|---|
| Agent | Apple container (a lightweight VM) | Firecracker microVM, reached over SSH | container, under gVisor `runsc` when Docker has it |
| Infra pair | two Apple containers | two Firecracker microVMs | two containers |
| Agent network | host-only agent network with pinned subnets | point-to-point TAP with a /31 per slot | an `--internal` /29 per agent, shared only with the gateway |
| Way out | Only the gateway is on the NAT egress network. An nft firewall in each agent's VM allows only the gateway. A host pf anchor drops agent traffic to the host and off the subnet. | `inet bot_bottle_fc` nft table: agent traffic to the gateway ports is DNAT'd to the gateway VM, everything else dropped | only the gateway is on an egress network |
| Control plane reachable from agent | No. The agent isn't on the control network. | No. There's no forward rule. | No. The agent isn't on the `--internal` control network. |
| Agents reach each other | No, by the firewall in each agent's VM. Refused at launch for a root-capable agent alongside others. | No. There's no forward rule. | No. There's no shared network. |
| Isolation probe | yes, as root and as the agent, plus a DNS check | yes | yes, plus a DNS check |
| Agent image builds | host `container build` | A published ext4 image. An overlay builds with host Docker or Podman when present (`BOT_BOTTLE_BUILD_ENGINE`), else with buildah in the orchestrator VM. A custom Dockerfile builds with buildah in the orchestrator VM. | host `docker build` |
| `nested_containers` | yes, rootless podman in the guest | rejected | rejected |
| Host requirements | `container` CLI, a one-time pf anchor and LaunchDaemon | `/dev/kvm`, `firecracker`, e2fsprogs, a one-time TAP and nft setup | Docker |

## Selection

The first match wins (`backend/selection.py`):

1. `--backend` or `BOT_BOTTLE_BACKEND`, if set.
2. **macos-container**, when Apple's `container` CLI is installed.
3. **firecracker**, on a KVM-capable Linux host, even before the
   `firecracker` binary is installed. Its preflight prints what's missing.
4. **docker**, after a warning and an interactive prompt. Headless runs exit
   instead of prompting.

## macos-container

Agents sit on a host-only network, fenced in by a host pf anchor and by an nft
firewall inside each agent's VM. The code is in `backend/macos_container/`.

Container names below are the live environment's. A named environment prefixes
`bb-<name>-` instead of `bot-bottle-` ([install.md](install.md#environments)).

- `bot-bottle-mac-orchestrator`: on the control network only. Sole opener of
  the DB volume.
- `bot-bottle-mac-infra`: the gateway, on three networks: NAT egress, the
  host-only agent network, and the control network. Apple has no container
  DNS, so it reaches the orchestrator by IP.

Agents are on the agent network only.

### The host firewall

A host pf anchor keeps agents off the Mac and off their subnet.

The agent network is `--internal` (vmnet host-only). That removes the internet
route but still lets guests reach the host's own addresses. With IP forwarding
on, anything that NATs the subnet would let them out.

So its subnets are pinned, and a host pf anchor, `com.apple/io.bot-bottle`
(`host_firewall.py`), drops traffic from them to the host itself and to
anything off the subnet. Agent-to-gateway traffic stays on the subnet.

- **Host address:** the anchor names the host as the subnet's first address.
  pf's `(self)` misses the vmnet bridge's own address.
- **Named environments:** the anchor is `io.bot-bottle.<name>`, each with its
  own subnet and IPv6 /64.
- **Install:** `install` installs the anchor and a root LaunchDaemon,
  `io.bot-bottle.pf`. It loads the anchor at boot and re-applies it every
  minute. `/etc/pf.conf` isn't edited.

#### IPv6

Nothing on the agent network uses IPv6, so the daemon turns it off on the agent
bridge.

The host's IPv6 addresses on the agent bridge can't be named in pf. They are
link-local, plus an autoconf one on the ULA prefix. The bridge's name also
changes whenever the network is recreated.

Each minute the daemon finds the bridge holding `192.168.128.1` and runs
`ifconfig <bridge> inet6 ifdisabled`. That runs host-side, so an agent can't
undo it. The IPv6 subnet is fixed: `fd62:6f74:6274:6c65::/64` (`gateway.py`).

#### The IPv4 subnet

The IPv4 subnet is recorded in the installed rules
(`host_firewall.agent_subnet`). Setup keeps it while nothing else on the Mac
uses it. Otherwise it picks the first free one of:

- `192.168.128.0/24`
- `192.168.200.0/24`
- `10.213.128.0/24`
- `172.30.128.0/24`

Setup checks interface addresses and routes other than vmnet bridges
(`conflicts`). A launch is refused while something else uses the subnet, with
setup as the remedy.

Setup lists what it changes before printing the `sudo` block. The daemon only
ever touches `bridge*` interfaces, so a LAN that happens to use the agent
subnet's first address is left alone.

#### Launch refusals

A launch is also refused when:

- an existing agent network has other subnets.
- the bridge still has IPv6 on. The launch waits out the daemon's minute first.
- the isolation probe finds the agent reaches the host's LAN address or
  `192.168.128.1`.

### Agents can't reach each other

Each agent's own kernel drops traffic to other agents, because pf never sees
it.

All agents share the agent network. Frames between them are switched on the
vmnet bridge without reaching the IP layer, so pf can't filter them. macOS's
bridge has no setting to hand them to pf.

Before anything else runs in the guest, bot-bottle loads an nftables table,
`inet bot_bottle`, as root (`agent_firewall.py`). The same exec points
`bot-bottle-gateway` at the gateway in `/etc/hosts`.

- **Out:** only the gateway's address is allowed, plus replies. Everything else
  is rejected with ICMP administratively prohibited, so a connection fails at
  once rather than timing out.
- **In:** only replies. Nothing connects into an agent on this backend. Control
  goes through `container exec`, and addresses are static, so no DHCP traffic
  is needed.
- **Root's outgoing traffic is exempt.** The agent runs as the image's user,
  and the launch's pf check runs as root. Incoming traffic has no exemption.

The table is kernel state, so it's loaded on every start of the guest. That
includes a kept guest restarted on resume. It's reloaded when reattach moves
the gateway.

The agent runs with `--cap-add CAP_NET_ADMIN` so root can load the table.
`container exec` can't grant a capability to one process. The agent's user has
no capabilities.

The claude, codex and pi images include `nftables`. An image without `nft`
launches with a warning and no firewall. A ruleset `nft` refuses fails the
launch.

#### An agent that can get root

**An agent that can get root can remove its firewall.** Its connections to
other agents then leave its VM. A sibling whose firewall is loaded still drops
them, since it accepts only replies.

So it reaches only agents that have no firewall or have removed theirs. pf
still keeps it off the Mac.

It could also change its own address, so only its identity token still
attributes its requests. It could spoof addresses on the shared network, for
example to take traffic meant for the gateway.

After the probe, the launch checks, as the agent's user, whether the agent:

- runs as root,
- has passwordless `sudo` or `doas`, or
- has a binary on `PATH` with file capabilities.

If it can get root:

- **Other agents are already running:** the launch is refused. The risk of
  cross-agent sabotage through a removed firewall outweighs the benefit of
  co-locating them.
- **No other agents are running:** the launch warns, since the risk is
  deferred rather than immediate.

The check can't prove an agent can't get root. It catches images built to
allow it.

Isolating such agents needs enforcement outside their VM, such as a network per
agent with a relay. `container` makes that costly today. It can't attach a
network to a running container, and vmnet doesn't route between its networks.

### Containers inside a bottle

```yaml
nested_containers: true
```

On macos-container this gives the agent `docker` and `docker compose`, backed
by a rootless podman service in the guest. It's a build and test convenience,
not a security boundary.

The podman service is guest-local. It starts after the bottle is registered and
exposes its Docker-compatible API socket, so the agent still runs `docker` and
`docker compose`.

Nothing is mounted from the host. Docker Desktop's socket stays out of the
bottle, and the guest gains no outer VM capabilities. The docker and
firecracker backends reject the flag rather than silently ignore it.

**Nested containers provide no isolation from the agent itself.** `root`
inside a nested container is the agent user outside it. The bottle remains the
boundary.

The reason is that rootless Docker doesn't work here at all. Apple Container's
capability bounding set omits `CAP_SYS_ADMIN`, which the kernel requires to
write a multi-range `uid_map`. Podman avoids that requirement by falling back
to a single-UID mapping.

#### Pulling images

Each registry needs an egress route, and so does the CDN it redirects layer
blobs to:

```yaml
nested_containers: true
egress:
  routes:
    # Docker Hub: registry, token endpoint, blob CDN.
    - host: registry-1.docker.io
      preserve_auth: true
      dlp: false
    - host: auth.docker.io
      preserve_auth: true
      dlp: false
    - host: production.cloudfront.docker.com
      dlp: false
    # GHCR: registry + blob CDN.
    - host: ghcr.io
      preserve_auth: true
      dlp: false
    - host: pkg-containers.githubusercontent.com
      dlp: false
    # quay.io: registry + blob CDNs. No preserve_auth needed for public pulls.
    - host: quay.io
      dlp: false
    - host: cdn01.quay.io
      dlp: false
    - host: cdn02.quay.io
      dlp: false
    - host: cdn03.quay.io
      dlp: false
```

Pulling images goes through the bottle's egress proxy like every other request.

- **The CDN route:** the CDN is a different host. Without its route the pull
  authenticates, fetches the manifest, then 403s partway through.
- **`preserve_auth`:** Docker Hub and GHCR need `preserve_auth: true`.
  Their token dance uses a client-fetched per-scope bearer token that the
  proxy would otherwise strip.
- **DLP off:** turn it off on every registry and CDN route. The bodies are
  compressed layer blobs that no detector can read. Buffering them is what can
  run the shared proxy out of memory.

`mcr.microsoft.com` and `registry.k8s.io` follow the same shape. They also
redirect blobs elsewhere: `*.data.mcr.microsoft.com` and `us-*-docker.pkg.dev`
respectively. Route whichever host the 403 names.

Route fields are in [manifest.md](manifest.md#egress-routes).

#### Testing egress from a nested container

Inside a nested container the same allowlist applies. An allowlisted host
returns 200, and anything else gets a 403 straight from the proxy.

The gateway's CA bundle and proxy settings are wired in automatically. So
`docker run … curl https://…` works with no extra flags (no `--add-host`, `-e`,
or `-v`).

Two things to know when testing that:

- **Public DNS fails by design.** Everything egresses through the proxy, so
  `nslookup` failing is expected and is not evidence of a problem.
- **Use `curl`, because BusyBox `wget` will lie to you.** Alpine's BusyBox
  `wget` drops the connection after the proxy's TLS interception and reports
  `error getting response`. The proxy still logs the decrypted request and
  returns a response.

## firecracker

Each agent is a Firecracker microVM on its own point-to-point TAP, and an nft
table lets it reach only its gateway. The code is in `backend/firecracker/`.

Networking is set up once with root by `scripts/firecracker-netpool.sh` or
`nix/firecracker-netpool.nix`. Both read `netpool.defaults.env`.

- **TAPs:** `bbfc{i}` per slot, owned by the user, with no shared bridge. The
  host side is `base + 2i`, the guest side `base + 2i + 1`.
- **Address block:** `10.243.0.0/16` by default, chosen to avoid Docker,
  libvirt, LANs and Tailscale. Override it with `BOT_BOTTLE_FC_IP_BASE`.
- **Isolation table:** `inet bot_bottle_fc` lets a VM reach only its own
  gateway, DNAT'd from the host end of its TAP. Preflight fails closed if the
  table is missing.

After the live pool, the setup reserves `BOT_BOTTLE_FC_ENV_SLOTS` (default 4)
slices for named environments. Slice `k` is `bb<k>fc*` TAPs from
`10.243.<k>.0`, tables `bot_bottle_fc<k>*` and infra links `bborch<k>`/`bbgw<k>`.

`netpool.env_slice` computes the slices. The math is in
[install.md](install.md#environments). Each environment's VMs and run
directories live apart (`util.env_dir`).

### The infra pair

The infra pair is two persistent VMs, each booting its own rootfs
(`infra_vm.py`, `infra_rootfs.py`):

- **Orchestrator VM:** the control plane, `registry.db` on a persistent
  `/dev/vdb` volume, the signing key, and buildah for in-VM agent builds.
- **Gateway VM:** the data plane, with a `gateway` JWT and the CA, and no
  build tooling. It reaches the orchestrator over the one forward rule its
  link allows.

### Agent builds in the orchestrator VM

The orchestrator VM's buildah builds every custom Dockerfile. It builds an
overlay when the host has no usable Docker or Podman.
`BOT_BOTTLE_BUILD_ENGINE=docker|podman` picks one, and `vm` always builds in
the VM.

Untrusted Dockerfile `RUN` steps built here share this VM with the control
plane. That's an accepted tradeoff for not needing host Docker.

It leans on the trust gate. A project agent's trust covers its Dockerfile and
overlay build context, so a change to them asks again before it is built
([architecture.md](architecture.md#state)).

### Agent VMs

Agent VMs run dropbear. The launcher reaches them over SSH with a per-bottle
key. Commands, file copies and freezing all go over SSH.

A guest rootfs keeps the image's owners and modes, setuid bits included. So
the agent user can't rewrite its own VM's system files or CA bundle.

[Internals](#internals) covers long commands over SSH, rootfs owners and the
guest environment.

## docker

Docker is the weakest boundary: agents share the host kernel unless gVisor is
registered. The code is in `backend/docker/`.

Names below are the live environment's. A named one uses `bb-<name>-` and its
own subnets ([install.md](install.md#environments)).

**gVisor.** When Docker has the `runsc` runtime registered, every agent
container runs under it, for a userspace syscall barrier. bot-bottle reads that
from `docker info` (`util.runsc_available`).

There's no manifest setting. The plan records it as `use_runsc` and the compose
file sets the runtime.

- `bot-bottle-orchestrator`: on the `--internal` control network, plus a
  loopback publish for the CLI.
- `bot-bottle-gateway`: on the shared `bot-bottle-gateway` network, the
  control network, and each running agent's own network. It reaches the
  orchestrator by Docker DNS.
- The gateway runs with `net.ipv4.ip_forward=0`, so it never routes between
  the agent networks it sits on.

### Per-agent networks

Each agent gets its own `--internal` network, so agents share no network and
can't reach each other. A sibling's address has no route.

The network is `bot-bottle-agent-<slug>` (`agent_network.py`). It's a /29 from
the agent subnet pool (`gateway_net.py`), holding only:

| Address | Holder |
|---|---|
| `.1` | docker's router |
| `.2` | the gateway |
| `.3` | the agent |

The agent's `.3` address is its attribution key. It's unique because the
subnets don't overlap.

One shared network with `enable_icc=false` wouldn't do. ICC off also drops
agent-to-gateway traffic on the bridge.

#### The subnet pool

The pool defaults to `10.242.0.0/20` (512 agents), below the shared network's
`10.242.255.0/24`. Override it with `BOT_BOTTLE_DOCKER_AGENT_SUBNETS` if that
range is used on your host.

- **Valid values:** an IPv4 network with no host bits set, a /29 or larger.
  Anything else is refused when the setting is read, naming the problem.
- **Restarts:** left unset or set to the default, it's omitted from the infra
  settings fingerprint, so it doesn't restart the infra pair. Another value
  restarts it once.

The launcher creates the network before registering, skipping every subnet a
docker network already uses. A subnet a concurrent launch took fails with an
overlap, and the next is tried.

#### Teardown

Teardown detaches the gateway before removing the network, since docker won't
remove a network with endpoints. That covers the launch's own teardown, an
adopted bottle's, and `cleanup`.

A recreated gateway has lost its agent-network attachments. Reattach joins it
to each running bottle's network at `.2` again.

A bottle launched before per-agent networks stays on the shared
`bot-bottle-gateway` network until it's relaunched.

#### Secrets

The agent's compose file holds only bare env names. Values such as
`ENV_VAR_SECRET` go through the subprocess env, never to disk.

## Isolation probe

Before the agent runs, the launcher proves the agent can't leave the box. It
doesn't trust configuration to describe the boundary (`backend/isolation_probe.py`).

| Check | macos-container | firecracker | docker |
|---|---|---|---|
| Connect to a canary listener on the host's primary address | yes | yes | yes |
| Connect to a public address | yes | yes | yes |
| Resolve a public name | yes | — | yes |
| Knock on up to four other running bottles | yes | — | yes |
| Run once as root, then as the agent's user | yes | — | — |

Any success is a leak, and the launch fails. The canary listener means a
"blocked" result is a real drop, not a connection refused.

### The sibling knock

The probe knocks on the other bottles' registered source IPs, from the
orchestrator, at TCP port 1, which nothing serves. With no listener there, a
refusal counts as a leak as well as a connection. Only silence or no route
passes.

This needs python3 or bash in the agent image, which can tell a refusal from a
drop. With only `nc` the probe is inconclusive and the launch fails.

The first bottle has no siblings to knock on. Each later launch checks itself
against those already running.

### Root, then the agent

On macos-container the probe runs twice:

1. As root, whose traffic the agent's firewall lets through, so it tests the
   host pf anchor.
2. As the agent's user, against its firewall, with the sibling knock.

### Timing

With python3 in the guest, the checks run at once. A launch then waits about
one timeout rather than their sum, since a blocked attempt is a drop that waits
it out. Without python3 they run one after another.

### How often

`BOT_BOTTLE_ISOLATION_CHECK` sets how often the probes run:

| Value | Runs |
|---|---|
| `once` (the default) | probes of host-wide state only when that state could have changed, and probes of a single agent's boundary on every launch |
| `always` | every probe on every launch |
| `off` | none, with a warning on each launch. The IPv6 wait above still runs. |

Under `once`, the host-wide probe is macOS's root probe of the pf anchor. It
runs again after a new boot, a reinstalled firewall (its rules file), or a new
agent network, host address or LAN address.

Those keys come from `host_firewall.isolation_key`. The keys it passed for are
kept in `<state root>/state/isolation-verified.json`. Between reboots, the pf
daemon reloads the anchor each minute.

Probes of a single agent's boundary run on every launch. They cover its own
firewall, its docker network, its VM's nft rules, and the sibling knock.

## The agent's home

A bottled agent's home lives on a volume of its own. A stop and a resume keep
it, while the guest itself starts fresh from the image.

The home is the guest user's home, `/home/node` in the current images. It holds
the workspace copy, the harness's conversations and config, and anything the
agent put there. The volume is `bot-bottle-home-<bottled agent>`
(`base.home_volume_name`).

| Backend | The home is | Filled |
|---|---|---|
| macos-container | a named `container volume` | It starts empty, so the first launch fills it from the image's home with a throwaway container (`launch._ensure_home`). |
| docker | an external named volume in the compose file, so `compose down --volumes` leaves it | Docker fills it from the image's home on first use. |
| firecracker | a sparse ext4 disk (`home.ext4`, 16G) in the bottled agent's state dir, the VM's second drive | The guest init fills it from the image's home on its first boot and mounts it over the home. |

### A kept home

When the home held an earlier run (`Bottle.home_kept`):

- the workspace isn't copied again. The copy empties the workspace first, and
  the agent's copy has its changes.
- the host controller starts the harness with its continue arguments
  (`AgentProvider.continue_args`).

| Harness | Continue arguments |
|---|---|
| claude | `claude --continue` |
| pi | `pi --continue` |
| codex | `codex resume --last` |

A home goes with its bottled agent's state. It's removed when its session ends
without being kept for resume, and in `cleanup bots`, which removes the homes
of bottled agents not kept.

Any harness that exits non-zero, as a stop does, is kept
(`bottle_state.capture_session_state`).

### Nothing in the home shadows the harness

The home outlives the image it was filled from, so nothing in it may shadow the
image's harness.

- On firecracker the agent's `PATH` lists the image's directories before the
  home's.
- claude runs with `DISABLE_AUTOUPDATER=1`, since its self-update writes
  `~/.local/bin/claude`.

An agent that exits non-zero within 30 seconds of a console launch or resume is
reported as `agent_errored`, with its exit status and last output. Exit status
127 means its command wasn't found.

A later non-zero exit is `agent_stopped`, with the same in `message`, so it can
still be resumed.

### The workspace's repo config

A copied git workspace brings its `.git`, but only the repo config git needs
locally, with credentials stripped (`git_config_filter.py`).

`.git/config` is rewritten from an allowlist. So is a submodule's under
`.git/modules/`, and a worktree's `config.worktree`.

Kept:

- `core.*`, except settings naming host paths or commands (`hooksPath`,
  `sshCommand`, `askPass`, `editor`, `pager`, `fsmonitor`, `worktree`, …)
- `extensions.*`, `user.name`/`email`, `alias.*`, `pull.*`, `lfs.*`, and a few
  plain `push`/`fetch`/`init`/`merge`/`diff` settings
- `branch.<b>.*` tracking, `remote.<r>.url`/`pushurl`/`fetch`/…, `submodule.<s>.*`
- plain `url.<base>.insteadOf` rewrites

Left on the host:

- `http.*`, such as an `extraHeader`
- `credential.*`
- `include`/`includeIf`
- a rewrite whose base carries a credential
- diff and merge drivers
- any value that still carries an auth header

Remote, push and submodule URLs lose their user info: an `https` token, or an
`ssh` password. A bare `git@` stays.

git in the bottle doesn't need any of the rest. It reaches upstreams through
git-gate with git-gate's credentials.

When a credential setting is left out, the launch says so, naming it but never
its value. The host's own config isn't changed (ADR 0016).

### A project's workspace disk (firecracker)

```sh
BOT_BOTTLE_FC_WORKSPACE_DISK=1     # or a size, such as 32G
```

On firecracker this keeps the workspace on a disk of its own, so what the agent
installed or built there survives to the next launch. The default size is 16G,
sparse (`backend/firecracker/workspace_disk.py`).

Without it, a new bottled agent starts with a fresh home on every backend. What
the agent installed or built in the workspace (`node_modules`, a `.venv`, a
build dir) is then gone the next time the project is launched.

**One disk per project, agent and bottle stack, per user.** It's
`<state root>/workspace-disks/<hash>.ext4`, keyed on the project's real path,
the agent, and the bottle names in launch order. A different bottle stack gets
its own disk, so nothing an agent left there runs in a bottle with other
permissions.

**Attached, never mounted on the host.** It's the VM's third drive
(`/dev/vdc`), which the guest init mounts at the workspace path from
`bb_workspace=` on the kernel command line. The exit patch comes back as
always, so the host repo stays the source of truth.

**One VM at a time.** A launch holds the disk's lock, and its owner is
recorded. While that owner's run dir exists, another launch of the project
warns and copies into its home as usual. A host controller restart doesn't
free it.

**What the next run sees.** The project as it is on the host now, plus what
the project ignores from the last run. In a git repo, the files git lists and
`.git` are removed and copied in again, since the last run's changes came back
as its patch.

Outside a git repo, everything outside the copy's skipped dirs is removed and
copied in again.

**Resume.** Resuming the bottled agent that used the disk last keeps the disk
untouched, like its home.

**Size.** A larger setting grows the disk at the next launch. It never shrinks.

**Cleanup.** The disks count as workspaces in the storage budget. `bot-bottle
cleanup workspaces` lists them and removes those whose project is gone. With
`--all` it removes every one not attached.

### Keeping the guest

```yaml
preserve_container: true
```

A bottle with this keeps the whole guest too, not just its home, so what the
agent installs outside its home stays. It's for an agent whose image gives it
root, such as an overlay that installs `sudo`.

It's off by default. The default images' agent user has no root, so its home
holds everything it can change.

Teardown keeps the guest when the bottled agent is kept for resume, and
removes it otherwise (`launch_common.keeps_guest`). Resume starts the kept one
(`reuses_guest`).

Resume redoes only the per-launch wiring: registration at its new address,
git-gate keys, the gateway CA, and the agent config.

- **macos-container:** the container is stopped, not removed, and `container
  start`ed on resume. It keeps the env it was made with, its `ENV_VAR_SECRET`
  too. The identity token was never in it, since it goes with each `exec`.
- **docker:** the container is stopped and taken off its agent network, which
  teardown removes as usual. Resume registers on a new agent network, connects
  the container there at its new address and starts it.
- **firecracker:** its root disk lives in the state dir (`guest.ext4`, grown
  to 16G sparse) instead of the run dir. Resume boots it again after writing
  this host's `/bb-init` and dropbear back over the ones in it.

On docker, the proxy env goes with each `docker exec`, never in the container's
env. That's the identity token and the gateway's address. So a kept container
has none to go stale.

On firecracker, `/bb-init` and dropbear are rewritten because an agent with
root could have changed them.

#### When a kept guest is replaced

A kept guest stays on the image it was made from. Resume notes when the agent's
image has moved on.

`bot-bottle resume --fresh <bottled agent>` starts a new guest from the current
image, its home kept. A kept guest that won't start again, its network gone
say, is replaced the same way.

#### Listing and cleanup

`bots --stopped` shows `kept` in its `CONTAINER` column. `cleanup bots` leaves
a kept guest, like its home and state.

`cleanup bots` also leaves the infra. The gateway, the orchestrator and their
networks share the bottles' resource prefix, but are listed by name and skipped
(`INFRA_CONTAINERS`, `INFRA_NETWORKS` in each backend's `infra.py`).

## Memory and vCPUs

A bottle's agent gets 2 GiB of memory on every backend, and 2 vCPUs on
firecracker. A bottle's `resources:` block changes either:

```yaml
resources:
  memory: 4G     # MiB as a number (4096), or "<n>M", "<n>MiB", "<n>G", "<n>GiB"
  cpus: 4        # whole vCPUs
```

- **`memory`:** 512 MiB to 64 GiB.
- **`cpus`:** 1 to 32.

Through `extends` and stacked `--bottle`s, each key is replaced by the later
bottle that sets it. The defaults live in `backend/bottle_size.py`.

| Backend | `memory` | `cpus` |
|---|---|---|
| firecracker | the VM's `mem_size_mib` | the VM's `vcpu_count`. Unset `cpus` is 2. |
| macos-container | `container run --memory <n>M` | `--cpus <n>` only when the bottle sets `cpus`, otherwise Apple's default |
| docker | the agent service's `mem_limit` | only when the bottle sets it, otherwise unlimited |

The host budget counts each running bottle at the memory it was given
([host-controller.md](host-controller.md#bottles-budget)).

## Infra images

The orchestrator and gateway run from a release's published images, pulled and
verified by digest. Building them from source is opt-in, never a fallback.

The images are OCI for docker and macos-container, and ext4 root filesystems
for firecracker. They're pulled from the pins in `published-artifacts.json`. A
pin is used only when its `inputs_hash` matches this install's sources
(`infra_image.py`).

### Where they come from

Releases publish to GHCR (`ghcr.io/diderisoft/<image>`), publicly, so pulling
needs no login.

GHCR stores only OCI content, so each ext4 rootfs is an OCI artifact beside its
image, `ghcr.io/diderisoft/<image>-rootfs:<version>-<arch>`. It has one gzip
layer of type `application/vnd.bot-bottle.rootfs.ext4+gzip`, and the empty
config.

The pin holds that reference and the layer's sha256. The host fetches the
layer by digest through the registry API (`oci_registry.py`) and checks it.
Older pins whose `url` is a plain HTTPS download of the ext4 file still work.

`BOT_BOTTLE_INFRA_ARTIFACT_TOKEN` is only for a private registry. It goes only
to `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN_HOST`.

### Building from source

```sh
bot-bottle up --build    # or BOT_BOTTLE_INFRA_BUILD=local for every command
```

docker and firecracker build with Docker. macos-container builds with Apple's
`container`.

Without the opt-in, an install with nothing published to match stops before
bring-up touches anything (`InfraService.require_infra_images`). An install
from `main` after the last release is one:

```
bot-bottle: error: this install (0.1.4 @ 1a2b3c4d5e6f, unreleased) has no published infra images.
    Install a release: curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
    Or build them locally (needs Docker): bot-bottle up --build
    (BOT_BOTTLE_INFRA_BUILD=local does the same for every command.)
```

The version is the package's. The commit comes from git in a checkout, or from
the install's `direct_url.json`. Re-running the installer installs the latest
release tag (`infra_image.RELEASE_INSTALL_HINT`).

### When `--build` sticks

| Command | Builds or pulls |
|---|---|
| a bring-up a launch triggers | keeps the last recorded bring-up's opt-in |
| `bot-bottle up` | does only what its flags say. Without `--build` it goes back to pulling, or stops. |

So `bot-bottle up --build` followed by `bot-bottle <agent>` keeps building.

One exception: a pair that's running, healthy and current, built locally by the
last `up --build`, on an install no release matches. An `up` (`--restart`
included) keeps it and its `--build` opt-in, and says so, since there's nothing
to pull and nothing needs building.

A cold start or `up --restart-infra` still needs a matching release or
`--build`.

## Gateway restarts

The orchestrator and gateway restart only for new images or changed settings.
When they do, running bottles lose the network for about ten seconds.

`bot-bottle up` adopts a running orchestrator and gateway when they're healthy
and booted the current images and settings. That holds with or without
`--restart`, which restarts the host controller.

Only new images (their inputs hash) or changed settings reboot them. `bot-bottle
up --restart-infra` forces it.

A reboot builds or downloads the new images first, while the old pair still
serves. Then it cuts over: running bottles lose the network for the ten or so
seconds the pair takes to boot and reattach them.

A harness's own retries usually cover that. Claude Code retries a failed
request up to ten times.

The CA agents trust doesn't change across reboots. The host keeps it and
pushes it into each new gateway. The gateway's first boot mints it.

So a running agent's process, which read its trust store once at start, keeps
trusting the gateway.

### Reattach

The gateway keeps no per-bottle state on disk except the CA. When the infra
pair comes up cold while bottles are running, `reattach_running_bottles`
restores each one:

- the gateway address
- the CA
- git-gate repos and credentials
- egress tokens

The orchestrator decrypts the tokens using the `ENV_VAR_SECRET` read back from
the agent. Each backend supplies a `GatewayAttacher` for the steps that differ.

Every bottle is attempted. Then any that failed to reattach abort the bring-up,
all named together. A bottle whose egress quietly starts failing TLS is worse
than a loud failure.

Two things keep that from wedging the host:

- **Host control still starts.** The orchestrator is up even when the reattach
  fails, so `bot-bottle up` still serves its host-control listener, and the
  console stays reachable to stop the bottle.
- **The override.** `bot-bottle up --allow-reattach-failures` names the bottles
  that won't reattach and brings the rest up. Without it, a "reattach pending"
  marker stays and the next bring-up retries the reattach.

To clear a bottle that won't reattach:

1. `bot-bottle up --allow-reattach-failures`.
2. Stop the bottle: Stop in the console, or quit its agent in `bot-bottle
   attach <bottle>`.
3. Resume it (`bot-bottle resume <bottle>`), which provisions it against the
   current gateway. If it's gone (its VM or container died), `bot-bottle
   cleanup bots` removes what's left.

## Internals

### Lifecycle

All three backends implement `BottleBackend` (`backend/base.py`). They share
the launch steps in `backend/launch_common.py`, `backend/provision_gateway.py`
and `backend/gateway_attach.py`.

| Method | Does |
|---|---|
| `prepare(spec, stage_dir, secrets)` | validates the manifest and resolves the plan: egress, git-gate, supervise, env, workspace, agent provider |
| `launch(plan)` | context manager: builds or loads images, ensures the infra pair, registers the bottle, starts the agent, runs the isolation probe, yields a `Bottle` |
| `provision(plan, bottle)` | installs the CA, skills, prompt and workspace into the running agent |
| `infra_service()` | the backend's `InfraService`: brings up or adopts the orchestrator and gateway |
| `prepare_cleanup()` / `cleanup(plan)` | find and remove leftover bottles, networks and VMs |
| `cache_entries()` | the cached images it keeps, each with a reason to stay or none, for `bot-bottle cleanup images` |
| `enumerate_active()` | running bottles, read from the runtime |
| `setup()` / `status()` / `teardown()` | one-time host setup, readiness, and infra removal |

`Freezer` (`backend/freeze.py`, with one subclass per backend) commits a
running bottled agent to an image that `bot-bottle resume` can start again.

### macos-container: exec and cp

Gateway files and commands go through the `container` CLI's exec and cp
(`gateway_transport.py`).

`container cp` writes under a volume's mount point, where the volume hides it.
So `cp_in` copies to `/tmp` and moves the file into place from inside.

### firecracker: long commands over SSH

dropbear refuses an exec request over its `MAX_CMD_LEN` (9000 bytes). So the
agent's command stages any argument over 1 KiB as a file under the guest's
`/root`. A long task prompt is the usual case.

The guest shell reads it back (`"$(cat …)"`). The command stays short however
long the prompt is.

### firecracker: rootfs owners

The tree is unpacked rootless, which maps every path to the host user. So its
tar headers are recorded as it's unpacked (`<tree>/.bb-owners`).

After `mke2fs -d`, one `debugfs -w` pass gives each inode the owner and mode
the image had. Files the image didn't hold, like the injected init and
dropbear, are root's (`rootfs_owners.py`).

A cached tree from before the record is rebuilt.

### firecracker: the guest environment

A VM has no process environment of its own. So every command runs under
`runuser`, with HOME, USER, PATH and the bottle's env set per invocation.

No value goes on the ssh command line, where any host process could read it:

- An `exec` script starts with the env's `export` lines, sent on its stdin.
- Before each agent command, the env is written over stdin to
  `/run/bot-bottle-agent/agent.env`. That file is on the guest's tmpfs, owned
  by root and readable by the agent's group.
- The guest shell sources it, then runs `env --chdir=<workdir> … <agent>`.

A built agent image (a custom Dockerfile or an overlay) records its `ENV` with
its rootfs in `.bb-image-env.json`, read at launch. It applies that under the
bottle's own env.

A published base, a built-in harness, has none. Its PATH entries are the
built-in default (`_DEFAULT_PATH_FOR`).
