# Backends

A backend runs the agent and the infra pair (orchestrator and gateway) with
one isolation primitive. All three implement `BottleBackend`
(`backend/base.py`) and share the launch steps in `backend/launch_common.py`,
`backend/provision_gateway.py` and `backend/gateway_attach.py`. They differ in
how the agent runs, how it gets a pinned address, and what stops it from
leaving the box except through the gateway.

## Selection

`backend/selection.py`, in order:

1. `--backend` or `BOT_BOTTLE_BACKEND`, if set.
2. **macos-container**, when Apple's `container` CLI is installed.
3. **firecracker**, on a KVM-capable Linux host, even before the
   `firecracker` binary is installed. Its preflight prints what's missing.
4. **docker**, after a warning and an interactive prompt. Headless runs exit
   instead of prompting.

## Lifecycle

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

## The agent's home

A bottled agent's home (the guest user's home, `/home/node` in the current
images, which holds the workspace copy, the harness's conversations and
config, and anything the agent put there) lives on a volume of its own,
`bot-bottle-home-<bottled agent>` (`base.home_volume_name`), so a stop and a
resume keep it while the guest itself starts fresh from the image:

- **macos-container:** a named `container volume`. It starts empty, so the
  first launch fills it from the image's home with a throwaway container
  (`launch._ensure_home`). `container cp` writes under a volume's mount
  point, where the volume hides it, so `cp_in` copies to `/tmp` and moves the
  file into place from inside.
- **docker:** an external named volume in the compose file, so `compose down
  --volumes` leaves it; docker fills it from the image's home on first use.
- **firecracker:** a sparse ext4 disk (`home.ext4`, 16G) in the bottled
  agent's state dir, the VM's second drive. The guest init fills it from the
  image's home on its first boot and mounts it over the home.

`Bottle.home_kept` says whether the home held an earlier run. If so, the
workspace isn't copied again (the copy starts with `rm -rf`, and the agent's
copy has its changes), and the host controller starts the harness with its
continue arguments (`AgentProvider.continue_args`: `claude --continue`, `pi
--continue`, `codex resume --last`). A home goes with its bottled agent's
state: when its session ends without being kept for resume (any harness that
exits non-zero, as a stop does, is kept; `bottle_state.capture_session_state`),
and in `cleanup bots`, which removes the homes of bottled agents not kept.

### Keeping the guest

A bottle with `preserve_container: true` keeps the whole guest too, for an
agent whose image gives it root (an overlay that installs `sudo`, say), so what
it installs outside its home stays. Off by default: the default images' agent
user has no root, so its home holds everything it can change. Teardown keeps
the guest when the bottled agent is kept for resume (`launch_common.keeps_guest`)
and removes it otherwise; resume starts the kept one (`reuses_guest`) and redoes
only the per-launch wiring (registration at its new address, git-gate keys, the
gateway CA, the agent config):

- **macos-container:** the container is stopped, not removed, and `container
  start`ed on resume. It keeps the env it was made with, its `ENV_VAR_SECRET`
  too; the identity token was never in it (it goes with each `exec`).
- **docker:** the container is stopped and taken off its agent network, which
  teardown removes as usual. Resume registers on a new agent network, connects
  the container there at its new address and starts it. Its proxy env (the
  identity token, the gateway's address) goes with each `docker exec`, never in
  the container's env, so a kept container has none to go stale.
- **firecracker:** its root disk lives in the state dir (`guest.ext4`, grown to
  16G sparse) instead of the run dir, and resume boots it again after writing
  this host's `/bb-init` and dropbear back over the ones in it (an agent with
  root could have changed them).

A kept guest stays on the image it was made from; resume notes when the
agent's image has moved on, and `bot-bottle resume --fresh <bottled agent>`
starts a new guest from the current image, its home kept. One that won't start
again (its network gone, say) is replaced the same way. `bots --stopped` shows
`kept` in its `CONTAINER` column; `cleanup bots` leaves a kept guest, like its
home and state.

## Comparison

| | macos-container | firecracker | docker |
|---|---|---|---|
| Agent | Apple container (a lightweight VM) | Firecracker microVM, reached over SSH | container, under gVisor `runsc` when Docker has it |
| Infra pair | two Apple containers | two Firecracker microVMs | two containers |
| Agent network | host-only agent network with pinned subnets | point-to-point TAP with a /31 per slot | an `--internal` /29 per agent, shared only with the gateway |
| Way out | only the gateway is on the NAT egress network; a host pf anchor drops agent traffic to the host and off the subnet | `inet bot_bottle_fc` nft table: agent traffic to the gateway ports is DNAT'd to the gateway VM, everything else dropped | only the gateway is on an egress network |
| Control plane reachable from agent | no; the agent isn't on the control network | no; no forward rule | no; the agent isn't on the `--internal` control network |
| Isolation probe | yes, plus a DNS check | yes | yes, plus a DNS check |
| Agent image builds | host `container build` | in the orchestrator VM with buildah, or a published ext4 image | host `docker build` |
| `nested_containers` | yes, rootless podman in the guest | rejected | rejected |
| Host requirements | `container` CLI, a one-time pf anchor and LaunchDaemon | `/dev/kvm`, `firecracker`, e2fsprogs, a one-time TAP and nft setup | Docker |

## macos-container

`backend/macos_container/`. Containers:

- `bot-bottle-mac-orchestrator`: on the control network only. Sole opener of
  the DB volume.
- `bot-bottle-mac-infra`: the gateway, on three networks: NAT egress, the
  host-only agent network, and the control network. Apple has no container
  DNS, so it reaches the orchestrator by IP.

Agents are on the agent network only. That network is `--internal` (vmnet
host-only), which removes the internet route but still lets guests reach the
host's own addresses, and with IP forwarding on, anything that NATs the subnet
would let them out. So its subnets are pinned and a host pf anchor,
`com.apple/io.bot-bottle` (`host_firewall.py`), drops traffic from them to the
host itself (named as the subnet's first address, since pf's `(self)` misses
the vmnet bridge's own address) and to anything off the subnet. Agent-to-gateway
traffic stays on the subnet. `install` installs the anchor and a root
LaunchDaemon (`io.bot-bottle.pf`) that loads it at boot and re-applies it every
minute; `/etc/pf.conf` isn't edited.

The host's IPv6 addresses on the agent bridge (link-local, and an autoconf one
on the ULA prefix) can't be named in pf, and the bridge's name changes whenever
the network is recreated. Nothing on the agent network uses IPv6, so each
minute the daemon finds the bridge holding `192.168.128.1` and turns IPv6 off
on it (`ifconfig <bridge> inet6 ifdisabled`, host-side, so an agent can't undo
it). The IPv6 subnet is fixed (`fd62:6f74:6274:6c65::/64`, `gateway.py`). The IPv4
one is recorded in the installed rules (`host_firewall.agent_subnet`): setup
keeps it while nothing else on the Mac uses it, and otherwise picks the first
free one of `192.168.128.0/24`, `192.168.200.0/24`, `10.213.128.0/24` and
`172.30.128.0/24`, checking interface addresses and routes other than vmnet
bridges (`conflicts`). A launch is refused while something else uses it, with
setup as the remedy. Setup lists what it changes before printing the `sudo`
block, and the daemon only ever touches `bridge*` interfaces, so a LAN that
happens to use the agent subnet's first address is left alone.

An existing agent network with other subnets is refused at launch; so is a
launch while the bridge still has IPv6 on (it waits out the daemon's minute
first), and the isolation probe refuses one where the agent reaches the host's
LAN address or `192.168.128.1`.

Gateway files and commands go through
the `container` CLI's exec and cp (`gateway_transport.py`). With
`nested_containers: true`, the guest runs rootless podman; see
[Containers inside a bottle](#containers-inside-a-bottle).

### Containers inside a bottle

A bottle may set `nested_containers: true`. On macos-container this starts a
guest-local, rootless **podman** service after the bottle is registered and
exposes its Docker-compatible API socket, so the agent still runs `docker` and
`docker compose`. Nothing is mounted from the host: Docker Desktop's socket
stays out of the bottle and the guest gains no outer VM capabilities. The
docker and firecracker backends reject the flag rather than silently ignore
it.

Rootless Docker doesn't work here at all: Apple Container's capability
bounding set omits `CAP_SYS_ADMIN`, which the kernel requires to write a
multi-range `uid_map`. Podman avoids that requirement by falling back to a
single-UID mapping, so nested containers provide **no isolation from the
agent itself**: `root` inside a nested container is the agent user outside
it. Nested containers are a build and test convenience, not a security
boundary. The bottle remains the boundary.

Pulling images goes through the bottle's egress proxy like every other
request, so each registry needs a route, **and so does the CDN it redirects
layer blobs to**, which is a different host. Without the CDN route the pull
authenticates, fetches the manifest, then 403s partway through. Docker Hub
and GHCR also need `inspect.preserve_auth: true`: their token dance uses a
client-fetched per-scope bearer token that the proxy would otherwise strip.
Turn DLP off on every registry and CDN route. The bodies are compressed layer
blobs that no detector can read, and buffering them is what triggers the
shared-proxy OOM in #455.

```yaml
nested_containers: true
egress:
  routes:
    # Docker Hub: registry, token endpoint, blob CDN.
    - host: registry-1.docker.io
      inspect: { preserve_auth: true, outbound_detectors: false, inbound_detectors: false }
    - host: auth.docker.io
      inspect: { preserve_auth: true, outbound_detectors: false, inbound_detectors: false }
    - host: production.cloudfront.docker.com
      inspect: { outbound_detectors: false, inbound_detectors: false }
    # GHCR: registry + blob CDN.
    - host: ghcr.io
      inspect: { preserve_auth: true, outbound_detectors: false, inbound_detectors: false }
    - host: pkg-containers.githubusercontent.com
      inspect: { outbound_detectors: false, inbound_detectors: false }
    # quay.io: registry + blob CDNs. No preserve_auth needed for public pulls.
    - host: quay.io
      inspect: { outbound_detectors: false, inbound_detectors: false }
    - host: cdn01.quay.io
      inspect: { outbound_detectors: false, inbound_detectors: false }
    - host: cdn02.quay.io
      inspect: { outbound_detectors: false, inbound_detectors: false }
    - host: cdn03.quay.io
      inspect: { outbound_detectors: false, inbound_detectors: false }
```

`mcr.microsoft.com` and `registry.k8s.io` follow the same shape and also
redirect blobs elsewhere (`*.data.mcr.microsoft.com` and
`us-*-docker.pkg.dev` respectively); route whichever host the 403 names.
Route fields are in [manifest.md](manifest.md#egress-routes).

Inside a nested container the same allowlist applies: an allowlisted host
returns 200 and anything else gets a 403 straight from the proxy. The
gateway's CA bundle and proxy settings are wired in automatically, so
`docker run … curl https://…` works with no extra flags (no `--add-host`,
`-e`, or `-v`).

Two things worth knowing when testing that:

- Public DNS inside a nested container fails **by design**. Everything
  egresses through the proxy, so `nslookup` failing is expected and is not
  evidence of a problem.
- Alpine's BusyBox `wget` drops the connection after the proxy's TLS
  interception and reports `error getting response`, even though the proxy
  logs the decrypted request and returns a response. Use `curl` to test
  egress; BusyBox `wget` will lie to you.

## firecracker

`backend/firecracker/`. Networking is set up once with root by
`scripts/firecracker-netpool.sh` or `nix/firecracker-netpool.nix`, both
reading `netpool.defaults.env`:

- TAP `bbfc{i}` per slot, owned by the user, with no shared bridge. Host side
  is `base + 2i`, guest side `base + 2i + 1`.
- The default block is `10.243.0.0/16`, chosen to avoid Docker, libvirt,
  LANs and Tailscale. Override it with `BOT_BOTTLE_FC_IP_BASE`.
- The `inet bot_bottle_fc` table lets a VM reach only its own gateway,
  DNAT'd from the host end of its TAP. Preflight fails closed if the table is
  missing.

The infra pair (`infra_vm.py`) is two persistent VMs, each booting its own
rootfs (`infra_rootfs.py`):

- **Orchestrator VM**: control plane, `bot-bottle.db` on a persistent
  `/dev/vdb` volume, the signing key, and buildah for in-VM agent builds.
  Untrusted Dockerfile `RUN` steps share this VM with the control plane.
  That's an accepted tradeoff for not needing host Docker. It leans on the
  trust gate: a project agent's trust covers its Dockerfile and overlay build
  context, so a change to them asks again before it is built
  ([architecture.md](architecture.md#state)).
- **Gateway VM**: the data plane, with a `gateway` JWT and the CA, and no
  build tooling. It reaches the orchestrator over the one forward rule its
  link allows.

Agent VMs run dropbear. The launcher reaches them over SSH with a per-bottle
key; commands, file copies and freezing all go over SSH.

A VM has no process environment of its own, so every command runs under
`runuser … env` with HOME, USER and PATH set per invocation. A built agent
image (a custom Dockerfile or an overlay) records its `ENV` with its rootfs
(`.bb-image-env.json`, read at launch) and applies it under the bottle's own
env. A published base, a built-in harness, has none; its PATH entries are
the built-in default (`_DEFAULT_PATH_FOR`).

## docker

`backend/docker/`. The weakest boundary: agents share the host kernel unless
gVisor is registered.

**gVisor.** When Docker has the `runsc` runtime registered
(`util.runsc_available`, read from `docker info`), every agent container runs
under it, for a userspace syscall barrier. There's no manifest setting; the
plan records it as `use_runsc` and the compose file sets the runtime.

- `bot-bottle-orchestrator`: on the `--internal` control network, plus a
  loopback publish for the CLI.
- `bot-bottle-gateway`: on the shared `bot-bottle-gateway` network, the
  control network, and each running agent's own network. It reaches the
  orchestrator by Docker DNS. It runs with `net.ipv4.ip_forward=0`, so it
  never routes between the agent networks it sits on.

Each agent gets its own `--internal` network, `bot-bottle-agent-<slug>`
(`agent_network.py`): a /29 from the agent subnet pool (`gateway_net.py`)
holding only docker's router (`.1`), the gateway (`.2`) and the agent (`.3`).
The pool defaults to `10.242.0.0/20` (512 agents), below the shared network's
`10.242.255.0/24`. Override it with `BOT_BOTTLE_DOCKER_AGENT_SUBNETS` if that
range is used on your host. It must be an IPv4 network with no host bits set,
a /29 or larger; anything else is refused when the setting is read, naming the
problem. Left unset or set to the default, it's omitted from the infra
settings fingerprint, so it doesn't restart the infra pair; another value
restarts it once. Agents
share no network, so they can't reach each other; a sibling's address has no
route. One shared network with `enable_icc=false` wouldn't do, since ICC off
also drops agent-to-gateway traffic on the bridge. The launcher creates the
network before registering, skipping every subnet a docker network already
uses; a subnet a concurrent launch took fails with an overlap and the next is
tried. The agent's `.3` address is its attribution key, unique because the
subnets don't overlap.

Teardown (the launch's own, an adopted bottle's, and `cleanup`) detaches the
gateway before removing the network, since docker won't remove a network with
endpoints. A recreated gateway has lost its agent-network attachments;
reattach joins it to each running bottle's network at `.2` again. A bottle
launched before per-agent networks stays on the shared `bot-bottle-gateway`
network until it's relaunched.

The agent's compose file holds only bare env names. Values such as
`ENV_VAR_SECRET` go through the subprocess env, never to disk.

## Isolation probe

`backend/isolation_probe.py`. The launcher doesn't trust configuration to
describe the boundary. Before the agent runs, it opens a canary TCP listener
on the host's primary address and has the guest try to connect to it and to a
public address. On docker it also tries to resolve a public name. Any success
is a leak, and the launch fails. The canary listener means a "blocked" result
is a real drop, not a connection refused.

On docker it also knocks on up to four other running bottles (their
registered source IPs, from the orchestrator) at TCP port 1, which nothing
serves. There's no listener there, so a refusal counts as a leak as well as a
connection; only silence or no route passes. This needs python3 or bash in
the agent image, which can tell a refusal from a drop; with only `nc` the
probe is inconclusive and the launch fails. The first bottle has no siblings
to knock on, and each later launch checks itself against those already
running.

## Gateway restarts

The gateway keeps no per-bottle state on disk except the CA. When the infra
pair comes up cold while bottles are running, `reattach_running_bottles`
restores each one: the gateway address, the CA, git-gate repos and
credentials, and egress tokens. Tokens are decrypted by the orchestrator using
the `ENV_VAR_SECRET` read back from the agent. Each backend supplies a
`GatewayAttacher` for the steps that differ. Any bottle that fails
to reattach aborts the bring-up, naming the bottle.
