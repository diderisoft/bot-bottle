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
| `cache_entries()` | the cached images it keeps, each with a reason to stay or none, for `bot-bottle prune` |
| `enumerate_active()` | running bottles, read from the runtime |
| `setup()` / `status()` / `teardown()` | one-time host setup, readiness, and infra removal |

`Freezer` (`backend/freeze.py`, with one subclass per backend) commits a
running agent to an image that `bot-bottle resume` can start again.

## Comparison

| | macos-container | firecracker | docker |
|---|---|---|---|
| Agent | Apple container (a lightweight VM) | Firecracker microVM, reached over SSH | container, under gVisor `runsc` when Docker has it |
| Infra pair | two Apple containers | two Firecracker microVMs | two containers |
| Agent network | host-only agent network with pinned subnets | point-to-point TAP with a /31 per slot | `bot-bottle-gateway` network with an explicit subnet |
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
traffic stays on the subnet. `backend setup` installs the anchor and a root
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
`nested_containers: true`, the guest runs rootless podman and exposes a
Docker-compatible socket. It provides no isolation from the agent, since root
in a nested container is the agent user. See the README for the egress routes
registries need.

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
  That's an accepted tradeoff for not needing host Docker.
- **Gateway VM**: the data plane, with a `gateway` JWT and the CA, and no
  build tooling. It reaches the orchestrator over the one forward rule its
  link allows.

Agent VMs run dropbear. The launcher reaches them over SSH with a per-bottle
key; commands, file copies and freezing all go over SSH.

## docker

`backend/docker/`. The weakest boundary: agents share the host kernel unless
gVisor is registered.

- `bot-bottle-orchestrator`: on the `--internal` control network, plus a
  loopback publish for the CLI.
- `bot-bottle-gateway`: on the agent-facing `bot-bottle-gateway` network and
  the control network. It reaches the orchestrator by Docker DNS.

Each agent attaches to the gateway network at a pinned address from
`gateway_net.py`, which is its attribution key. The agent's compose file
holds only bare env names. Values such as `ENV_VAR_SECRET` go through the
subprocess env, never to disk.

## Isolation probe

`backend/isolation_probe.py`. The launcher doesn't trust configuration to
describe the boundary. Before the agent runs, it opens a canary TCP listener
on the host's primary address and has the guest try to connect to it and to a
public address. On docker it also tries to resolve a public name. Any success
is a leak, and the launch fails. The canary listener means a "blocked" result
is a real drop, not a connection refused.

## Gateway restarts

The gateway keeps no per-bottle state on disk except the CA. When the infra
pair comes up cold while bottles are running, `reattach_running_bottles`
restores each one: the gateway address, the CA, git-gate repos and
credentials, and egress tokens. Tokens are decrypted by the orchestrator using
the `ENV_VAR_SECRET` read back from the agent. Each backend supplies a
`GatewayAttacher` for the steps that differ. Any bottle that fails
to reattach aborts the bring-up, naming the bottle.
