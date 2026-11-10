# Install

How bot-bottle gets onto a host, what each backend needs there, and how it
comes off again. The backends themselves are in [backends.md](backends.md).

## Installer

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

`install.sh` is a bootstrapper. It installs the latest release: the newest
`vX.Y.Z` tag, looked up through GitHub's tags API (it fails, rather than
installing `main`, when it can't find one). It finds a suitable Python, installs
bot-bottle with its `remote` extra for phone pairing (none on Intel Macs,
which iroh has no build for) using `uv` or `pipx` if you have either, or a
private venv at `~/.bot-bottle/venv`, creates `~/.bot-bottle`, and runs
`bot-bottle bootstrap`: `install` (the backend's host setup, tmux,
autostart), `setup` and `doctor`, reading from your terminal under `curl | sh`.
It is idempotent and never runs `sudo` on its own: the backend's one root step
is asked about first ([Host setup and sudo](#host-setup-and-sudo)). It shows one line per step (a spinner on a terminal) and keeps the
package tool's own output in `~/.bot-bottle/logs/install-<time>.log`, printing
its end if a step fails. With `BOT_BOTTLE_ENV` set it says which environment it
sets up. It ends with doctor's `next:` line, the one thing to do next on this
host.

Python-native users can skip it, pinning the latest tag (`v0.1.6` here; use
the newest one listed under the repository's tags):

```sh
uv tool install "bot-bottle[remote] @ git+https://github.com/diderisoft/bot-bottle.git@v0.1.6"
# or the same with: pipx install
```

To install `main` instead, pass `--main` (`… | sh -s -- --main`) or set
`BOT_BOTTLE_INSTALL_REF=main`, then build the infra images (below).

The installer's settings (`BOT_BOTTLE_PYTHON`, `BOT_BOTTLE_VENV`,
`BOT_BOTTLE_INSTALL_SPEC`, `BOT_BOTTLE_INSTALL_REF`, all optional) are in
[Settings](#settings).

### Releases and infra images

The normal path is a release: its orchestrator and gateway images are
published publicly on GHCR, and bot-bottle pulls and verifies them, with no
registry login ([backends.md](backends.md#infra-images)). Each release is tagged
`vX.Y.Z` on the commit that pins its images, and both commands above install a
tag. `main` matches the last release only until a change to the orchestrator or
gateway lands after it. An install with no published images to match stops on
its first bring-up and says so; it never builds them on its own
([backends.md](backends.md#infra-images)). To run from `main` (developing
bot-bottle, or a fix not yet released), build them locally:

```sh
bot-bottle up --build    # or set BOT_BOTTLE_INFRA_BUILD=local
```

That needs Docker on docker and firecracker hosts, and Apple's `container` on
macOS. Launches after it keep building locally until an `up` without
`--build` (which keeps a running, current local build rather than stopping, as
long as no release matches the install).

## Requirements

### Python

**Python ≥ 3.11**, or nothing. With no suitable Python the installer has uv
supply Python 3.13: your uv if you have one, else a pinned uv release it
downloads, checks against its published SHA-256 and keeps privately under
`~/.bot-bottle/uv` with that Python and bot-bottle itself, where
`bot-bottle uninstall` removes them.

When you do have Python, this is what trips people up on macOS: the `python3`
Apple ships at `/usr/bin/python3` is **3.9.6**, which is too old. Bare
`python3` resolves to that stub far more often than people expect.
`path_helper` builds a login shell's `PATH` from `/etc/paths` and then appends
`/etc/paths.d/*`, and `/usr/bin` sits in the former, so even when
`/opt/homebrew/bin` *is* on the `PATH` (via `/etc/paths.d/homebrew`), it comes
after `/usr/bin` and loses. Prepending a newer Python is something your shell
profile does, and a fresh account, a launchd job, or a CI runner has no such
profile.

So the installer looks past bare `python3` before giving up. It tries
`python3`, then the versioned `python3.11`–`python3.14` names, then
`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, and python.org
framework builds, and says which one it picked when it isn't the obvious one.
Point it somewhere specific with `BOT_BOTTLE_PYTHON=/path/to/python3`.

### No uv or pipx required

If either is present the installer uses it (`uv` first) and stays out of the
way. If neither is, bot-bottle installs into a private venv at
`~/.bot-bottle/venv` (override with `BOT_BOTTLE_VENV`) and symlinks the entry
point into `~/.local/bin`.

There is deliberately no `pip install --user` path. Homebrew, python.org and
Debian/Ubuntu interpreters are all externally managed (PEP 668), which blocks
`--user` outright, so on a Mac it is never the fallback it appears to be. A
venv is exempt from PEP 668, and `venv` is stdlib, so unlike `uv` or `pipx`
there is nothing to bootstrap first.

### git and tmux

- **git**, because the default install spec is a `git+` URL. Set
  `BOT_BOTTLE_INSTALL_SPEC` to a wheel path or index name to avoid it.
- **tmux 3.1 or newer**, on every backend. It runs the agent sessions (see
  [host-controller.md](host-controller.md#sessions)).

### A backend

The installer deliberately does *not* install a backend; `bot-bottle doctor`
reports what's missing afterwards. Which backend a host gets is in
[backends.md](backends.md#selection).

`doctor` checks that backend in full, the one a launch here would use
(`BOT_BOTTLE_BACKEND`, else the one detected), and shows its diagnostics when
it isn't ready; it decides doctor's exit code. Any other backend gets one
`note:` line, ready or not, and is never offered for install
(`doctor --verbose` shows every backend's diagnostics; `doctor --backend=X`
checks X alone). `doctor` ends with `next:`, the most pressing of: restart the
host controller (`up --restart`), set up the backend (`install --backend=…`),
write starters (`setup`), start the services (`up`), else run `bot-bottle`.
`doctor --next` prints only that.

| Backend | Needs |
|---|---|
| macos-container (macOS default) | Apple's `container` CLI, and the one-time host firewall. No Docker. |
| firecracker (Linux default with KVM) | `/dev/kvm`, the `firecracker` binary, e2fsprogs, and the one-time network pool. Docker only for `bot-bottle up --build` (infra images from source instead of a release's). |
| docker (fallback) | Docker |

#### Host setup and sudo

Each backend's one-time host setup has one step that needs root: the macOS
host firewall, or firecracker's network pool on a systemd host. Run on a
terminal, `bot-bottle install` (and `bootstrap`, and the installer) says what
that step is and the exact command, and asks first:

```
bot-bottle needs root once, to install the host firewall (a pf anchor, reloaded at boot).
It would run: sudo /Users/you/.local/bin/bot-bottle install --backend=macos-container
Run it now? [y/N]
```

Yes runs it (sudo asks for your password); no, or no terminal, prints the
commands for you to run instead, as below. It isn't asked once the step is in
place, nor on NixOS (the pool is a module you import) or a Linux host without
systemd (a script you run yourself).

#### macOS: Apple Container

`bot-bottle install --backend=macos-container` offers to install, or prints the
`sudo` commands that install, the host firewall: a pf anchor and a LaunchDaemon that reloads it at
boot ([backends.md](backends.md#macos-container)). Without it, launches are
refused.

Run as root through `sudo`, bot-bottle writes only those system pieces. It
doesn't register the environment or create its database, so no root-owned
state lands in your home; your next ordinary run does that. If an older version
left some (unprivileged commands then fail on permissions), every command but
`doctor` stops and names the fix, which `doctor` reports too:
`sudo chown -R "$(id -un):$(id -gn)" <the paths it lists>`.

#### Linux: Firecracker

A KVM-capable Linux host defaults to the Firecracker backend. It needs:

- **`/dev/kvm`** present and accessible. Load `kvm-intel` or `kvm-amd` (and
  enable virtualization in BIOS/firmware). The invoking user must be in the
  `kvm` group: `sudo usermod -aG kvm "$USER"`, then log in again. bot-bottle
  preflights this and reports exactly what's missing.
- **`firecracker`** on `PATH`: take a release from
  <https://github.com/firecracker-microvm/firecracker/releases>. Start flows
  print this pointer when the binary is missing.
- **e2fsprogs** (`mke2fs`, `debugfs`, `e2fsck`, `resize2fs`) for building root
  filesystems. Docker is optional: it is used only by `bot-bottle up
  --build`, which builds the infra images from source rather than downloading
  a release's prebuilt ones.
- **A one-time privileged network setup**: the per-bottle TAP pool plus the
  fail-closed `nftables` isolation table. `bot-bottle install
  --backend=firecracker` gives the host-appropriate config (a NixOS module, a
  `sudo` script elsewhere) plus autostart for the host controller.
  `bot-bottle doctor --backend=firecracker` reports what's present, including
  whether the pool range collides with an existing route. The pool defaults to
  `10.243.0.0/16`, an obscure RFC-1918 block that dodges Docker, libvirt, LANs
  and, deliberately, Tailscale's `100.64.0.0/10` CGNAT range. Override it with
  `BOT_BOTTLE_FC_IP_BASE` if it clashes on your host. The setup also reserves
  slices for named environments ([Environments](#environments)).

```sh
BOT_BOTTLE_BACKEND=firecracker bot-bottle start <agent>
```

**NixOS.** Load the KVM module (`boot.kernelModules = [ "kvm-intel" ];` or
`kvm-amd`) and add your user to the `kvm` group. Docker
(`virtualisation.docker`, plus the `docker` group) is only needed to build the
infra images from source (`bot-bottle up --build`). For the network pool, use the flake module, then
`nixos-rebuild switch`; imperative nft and TAP rules don't survive a rebuild.

```nix
imports = [ inputs.bot-bottle.nixosModules.firecracker-netpool ];
services.bot-bottle-firecracker = { enable = true; owner = "you"; };
```

Channel users can `imports = [ <bot-bottle>/nix/firecracker-netpool.nix ]`
instead. `firecracker` isn't in nixpkgs as a user binary by default: install
the release binary (pin the version) and put it on `PATH`.

#### Docker

The fallback for CI and hosts without Apple Container or KVM. Ask for it
explicitly:

```sh
BOT_BOTTLE_BACKEND=docker bot-bottle start <agent>   # or --backend=docker
```

It is the weakest boundary; see [backends.md](backends.md#docker).

### Signing in an agent

A Claude agent signs in one of two ways:

- your host's Claude Code login, with
  `agent_provider.forward_host_credentials: true`. `bot-bottle setup` writes
  this when Claude Code is installed and logged in.
- a long-lived token from `claude setup-token`, kept in a host variable that
  `agent_provider.auth_token` names.

Either way the gateway injects it; the agent only ever sees a placeholder
([host-controller.md](host-controller.md#forwarded-host-logins)). The manifest
fields are in [manifest.md](manifest.md#agents).

## First run

```sh
bot-bottle start <agent>   # pulls the images on first run, then drops you into the agent
```

From `main` rather than a release, run `bot-bottle up --build` first
([Releases and infra images](#releases-and-infra-images)).

## Environments

One host can run several independent installs side by side. `BOT_BOTTLE_ENV`
picks one: unset (or `live`) is the normal install; a name such as `dev` or
`ci-6839` (lowercase letters, digits and `-`, at most 24) is a separate
environment with its own state, names and network, so working on bot-bottle or
running CI never touches the live install (`bot_bottle/environment.py`).

| | `live` | a named environment `<name>` |
|---|---|---|
| State root | `~/.bot-bottle` | `~/.bot-bottle-envs/<name>` |
| Containers, VMs, networks, volumes | `bot-bottle-*` | `bb-<name>-*` |
| Host controller | `bot-bottle-hostctl.service` / `io.bot-bottle.hostctl` | `bb-<name>-hostctl.service` / `io.bot-bottle.<name>.hostctl` |
| docker gateway network | `10.242.255.0/24` | a free /24 of `10.241.0.0/16`, allocated and recorded |
| docker agent /29s | `10.242.0.0/20` | `10.240.0.0/16`, shared by named environments (allocation skips any subnet in use) |
| docker orchestrator host port | `127.0.0.1:8099` | a free port of 18100-18899, allocated (moves on when docker says the port is taken) and recorded per container under the state root |
| firecracker pool | slice 0 (`bbfc*`, `bot_bottle_fc`, `bborch0`/`bbgw0`) | the slice it claims (below) |
| firecracker run and infra dirs | `~/.cache/bot-bottle/firecracker/{run,infra}` | `~/.cache/bot-bottle/firecracker/envs/<name>/{run,infra}` |
| macOS pf anchor | `com.apple/io.bot-bottle` | `com.apple/io.bot-bottle.<name>` |
| macOS agent IPv6 /64 | `fd62:6f74:6274:6c65::/64` | a free `fd62:6f74:6275:<n>::/64`, allocated and recorded |

Explicit settings still win: `BOT_BOTTLE_ROOT`, `BOT_BOTTLE_DOCKER_*` and
`BOT_BOTTLE_FC_*` override what the environment derives. Image, artifact and
root-filesystem caches are content-addressed and shared by every environment.
`live` derives exactly the names, paths and settings fingerprint an install
had before environments existed.

A named environment registers itself (`~/.bot-bottle-envs/<name>/env.json`)
the first time a command runs in it. A name whose resource prefix would match
another registered environment's (`dev` and `dev-2`: `bb-dev-` matches
`bb-dev-2-orchestrator`) is refused. `doctor` and `status` print the active
environment and its orchestrator, and warn about any other bot-bottle
orchestrator answering.

An environment runs one backend's orchestrator and gateway at a time: its
bring-up records which, and bringing up another backend's while that one
answers is refused ([architecture.md](architecture.md#components)).

```sh
bot-bottle envs                  # every environment, its root, and what runs
bot-bottle envs rm dev           # tear dev down and delete its state
bot-bottle envs rm dev --dry-run # what it would remove
```

`envs rm` runs the teardown as that environment: the bottles its host
controller holds, each available backend's leftover bottles, orchestrator,
gateway, containers, networks and volumes (not images), and its host
controller and autostart unit. Then it deletes the state root and firecracker
directory and frees the pool slice. If part of the teardown fails, the state is
kept so it can be retried; `--force` deletes it anyway. It refuses `live`; use
`uninstall`, which in turn refuses to run in a named environment and removes
only live's containers and networks, so remove named environments first. A root set
with `BOT_BOTTLE_ROOT` outside `~/.bot-bottle-envs/` is left in place.

**Firecracker slices.** TAPs need root, so the network-pool setup reserves the
live pool plus `BOT_BOTTLE_FC_ENV_SLOTS` (default 4; NixOS `envSlots`) slices
up front, and a named environment claims the lowest free one on first use with
no root. Slice `k` (live is 0) has the live pool's size and:

| | live (`k = 0`) | slice `k` (1..63) |
|---|---|---|
| agent /31s | from `10.243.0.0` | from `10.243.<k>.0` (base + 256k) |
| agent TAPs | `bbfc0..` | `bb<k>fc0..` |
| nft tables | `bot_bottle_fc`, `_nat`, `_gw` | `bot_bottle_fc<k>`, `_nat`, `_gw` |
| orchestrator link | `bborch0`, host `10.243.255.0` | `bborch<k>`, host `10.243.255.<4k>` |
| gateway link | `bbgw0`, host `10.243.255.2` | `bbgw<k>`, host `10.243.255.<4k+2>` |

Each slice costs pool size + 2 TAPs. No slice's TAP wildcard (`bbfc*`,
`bb<k>fc*`) matches another's, so each slice's nft tables isolate only their
own VMs. With every slice held, a named environment stops and says to remove
one (`envs rm`) or raise `BOT_BOTTLE_FC_ENV_SLOTS` and rerun the pool setup.
Claims are host-wide, so two users' environments (a developer's `dev`, a CI
runner's `ci-<run>`) never share a slice: one file per claimed slice in
`/var/lib/bot-bottle-fc/slices` (`BOT_BOTTLE_FC_SLICES_DIR`, NixOS `slicesDir`),
which the pool setup creates mode 2775 for the pool's group
(`BOT_BOTTLE_FC_GROUP` / NixOS `group`, else the owner's primary group). Every
user that runs bot-bottle must be in that group. A claim names its
environment, user and registration file, is created atomically under a lock,
and is replaced only when stale: its registration is confirmed gone and none
of the slice's TAPs is in use. A live claim is never taken; `envs rm` releases
it (`backend/firecracker/slice_claims.py`).

**Docker and macOS subnets.** A named environment allocates its docker gateway
/24: it tries the `10.241.0.0/16` /24s in an order that starts at its name's
hash, skips any a docker network already has, and moves on when docker reports
a pool overlap (another environment got there first). The one it gets is
recorded in its state root (`docker-gateway-subnet`), so it's stable across
restarts, and an existing gateway network's subnet is adopted. The macOS agent
IPv6 /64 is allocated the same way at setup, skipping the ones other
environments' anchors block, and recorded in the installed pf rules.

That docker port is the only host port bot-bottle publishes: the docker
gateway, macOS containers and firecracker VMs are reached on their own
networks, never through a host port, so they can't collide.

The host controller's loopback port is ephemeral and published under each
environment's own root. Its guest listener keeps port 8098 in every
environment because it binds only that environment's own link address (its
orchestrator TAP, docker publish bridge or vmnet control gateway), and the
NixOS module opens 8098 on every slice's orchestrator TAP.

**macOS.** Each environment has its own pf anchor, so a named environment
needs its own one-time `sudo env BOT_BOTTLE_ENV=<name> bot-bottle install
--backend=macos-container`. Setup skips agent subnets other environments'
anchors already claim.

## Settings

Every `BOT_BOTTLE_*` variable you might set, all optional. Unset or empty
means the default. The ones bot-bottle sets for its own processes are in
[runtime-contract.md](runtime-contract.md#internal-environment).

Generated from `bot_bottle/env_vars.py`: edit that, then run
`python3 scripts/gen_env_docs.py`.

<!-- env-vars:settings:begin -->
| Variable | For | Does | Default |
|---|---|---|---|
| `BOT_BOTTLE_BACKEND` | user | Backend to use: `macos-container`, `firecracker` or `docker`. Same as `--backend`. | the host default |
| `BOT_BOTTLE_ENV` | user | The environment: `live`, or a name (lowercase letters, digits and `-`, at most 24) such as `dev` or `ci-<run>`. A named one gets its own state root, `bb-<name>-*` container, VM, network and volume names, docker subnets and firecracker pool slice, so it never touches the live install; explicit settings below still win. See `bot-bottle envs`. | `live` |
| `BOT_BOTTLE_ROOT` | user | State root: manifests, keys, secrets, the host DB and audit journal. | `~/.bot-bottle`; `~/.bot-bottle-envs/<name>` in a named environment |
| `BOT_BOTTLE_LOG_LEVEL` | user | `debug`, `info`, `warn` or `error`. `debug` also turns on the orchestrator's access log. | `info` |
| `BOT_BOTTLE_INFRA_BUILD` | user | `local` builds the orchestrator and gateway images from source instead of pulling a release's, for every command; `bot-bottle up --build` is the flag form. Building is never automatic: an install with no published images that match (e.g. from `main`) stops without it. | pull |
| `BOT_BOTTLE_BUILD_ENGINE` | user | Image build engine: `docker`, `podman` or `vm`. | the first usable one |
| `BOT_BOTTLE_ARTIFACT_CACHE` | user | Where verified artifact downloads (infra rootfs, image layers) are cached. | `~/.cache/bot-bottle/artifacts` |
| `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN` | user | Token for pulling from a private image registry (sent only to `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN_HOST`), or with `write:packages` publishing to the release registry. Published images are public, so pulling them needs none. | — |
| `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN_HOST` | user | The one registry host `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN` is sent to when pulling. Unset, it is sent nowhere. | unset |
| `BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS` | user | How long a bottle's teardown waits on the orchestrator. Read by the process that runs the launch (the host controller), so set it in that process's environment. | `30` |
| `BOT_BOTTLE_DOCKER_GATEWAY_SUBNET` | user | docker: the gateway network's subnet. | `10.242.255.0/24`; a named environment allocates a free /24 of `10.241.0.0/16` and records it |
| `BOT_BOTTLE_DOCKER_AGENT_SUBNETS` | user | docker: the range per-agent networks are carved from, as /29s. | `10.242.0.0/20`; `10.240.0.0/16` in a named environment |
| `BOT_BOTTLE_MACOS_CONTAINER_DNS` | user | macos-container: DNS server for the gateway, orchestrator and image builder. When set, a builder running with other DNS is restarted with it. | the host's IPv4 resolver, else `1.1.1.1` |
| `BOT_BOTTLE_FC_CACHE` | user | firecracker: cache root for the kernel, root filesystems and run dirs. | `~/.cache/bot-bottle/firecracker` |
| `BOT_BOTTLE_START_AUTO` | user | `1` makes a guided `bot-bottle start` run the suggested bottle without asking (`--auto`); `--no-auto` asks for one run. See bottles-suggest.md. | off |
| `BOT_BOTTLE_ARCHITECT_ALWAYS_ON` | user | The bottle architect's bottle boots with the host controller and stays up; `0` boots it on demand and lets it idle out (`BOT_BOTTLE_ARCHITECT_IDLE`). See bottles-suggest.md. | on |
| `BOT_BOTTLE_ARCHITECT_IDLE` | user | When the architect isn't always on: seconds its bottle stays up after its last suggestion; `0` stops it after each one. | `600` |
| `BOT_BOTTLE_ISOLATION_CHECK` | user | The launch's isolation probes, which prove an agent can reach only the gateway: `always` on every launch; `once` probes host-wide state (macOS's pf) again only after a reboot, a firewall reinstall or a network change, and an agent's own firewall every launch; `off` skips them (a warning each launch). See backends.md. | `once` |
| `BOT_BOTTLE_FC_WORKSPACE_DISK` | user | firecracker: keep the project's workspace on a per-project disk between runs, so what the project ignores (`node_modules`, build dirs) survives. `1` for the default size, or a size like `32G`. See backends.md. | off |
| `BOT_BOTTLE_FC_KERNEL` | user | firecracker: the guest kernel. | `vmlinux` in the firecracker cache |
| `BOT_BOTTLE_FC_DROPBEAR` | user | firecracker: the static dropbear put in every guest as its SSH server. | `dropbear` in the firecracker cache |
| `BOT_BOTTLE_FC_POOL_SIZE` | user | firecracker: TAP pool slots, so concurrent bottles. Must match the host's network pool. | `8` |
| `BOT_BOTTLE_FC_ENV_SLOTS` | user | firecracker: network-pool slices the host reserves for named environments, after the live pool. Must be at most the host's. | `4` |
| `BOT_BOTTLE_FC_SLICES_DIR` | user | firecracker: the host-wide claims on those slices, created by the network pool setup for the pool's group. Must match the host's. | `/var/lib/bot-bottle-fc/slices` |
| `BOT_BOTTLE_FC_IP_BASE` | user | firecracker: base IPv4 of the pool's /31s. Must match the host's network pool. | `10.243.0.0`; `10.243.<k>.0` for a named environment's slice k |
| `BOT_BOTTLE_FC_IFACE_PREFIX` | user | firecracker: agent TAP name prefix. Must match the host's network pool. | `bbfc`; `bb<k>fc` for slice k |
| `BOT_BOTTLE_FC_NFT_TABLE` | user | firecracker: the nftables isolation table. Must match the host's network pool. | `bot_bottle_fc`; `bot_bottle_fc<k>` for slice k |
| `BOT_BOTTLE_FC_ORCH_IFACE` | user | firecracker: the orchestrator VM's TAP. Must match the host's network pool. | `bborch0`; `bborch<k>` for slice k |
| `BOT_BOTTLE_FC_GW_IFACE` | user | firecracker: the gateway VM's TAP. Must match the host's network pool. | `bbgw0`; `bbgw<k>` for slice k |
| `BOT_BOTTLE_FC_OWNER` | user | firecracker network pool script: the user that owns the TAPs. | `$SUDO_USER`, else `$USER` |
| `BOT_BOTTLE_FC_GROUP` | user | firecracker network pool script: a group to own the TAPs instead of one user. | — |
| `BOT_BOTTLE_PYTHON` | user | Installer: the interpreter to install with; skips the search. | — |
| `BOT_BOTTLE_VENV` | user | Installer and `uninstall`: where the venv install lives when there's no `uv` or `pipx`. | `~/.bot-bottle/venv` |
| `BOT_BOTTLE_INSTALL_SPEC` | user | Installer: the pip spec to install instead of the default `git+` URL at the latest release tag, such as a wheel path or index name. | — |
| `BOT_BOTTLE_INSTALL_REF` | user | Installer: the git ref to install instead of the latest release tag, such as `v0.1.4`, or `main` (same as `--main`; then run `bot-bottle up --build`). | the latest `vX.Y.Z` tag |
| `BOT_BOTTLE_ORCHESTRATOR_IMAGE` | dev | The orchestrator image to run. | `bot-bottle-orchestrator:latest` |
| `BOT_BOTTLE_GATEWAY_IMAGE` | dev | The gateway image to run. | `bot-bottle-gateway:latest` |
| `BOT_BOTTLE_HOSTCTL_PORT` | dev | The host controller's loopback port. | ephemeral |
| `BOT_BOTTLE_DOCKER_CA_MOUNT` | dev | docker in CI: a named volume for the gateway CA, for a runner that shares the host's Docker socket. Removed at the GitHub cutover. | — |
| `BOT_BOTTLE_DOCKER_ROOT_MOUNT` | dev | docker in CI: a named volume for the state root the orchestrator mounts. Removed at the GitHub cutover. | — |
| `BOT_BOTTLE_DOCKER_CLIENT_NETWORK` | dev | docker in CI: the job container's network, which the orchestrator joins. Removed at the GitHub cutover. | — |
| `BOT_BOTTLE_INFRA_ARTIFACT_BASE` | release | The registry the images and rootfs artifacts are published to. | `https://ghcr.io` |
| `BOT_BOTTLE_INFRA_ARTIFACT_OWNER` | release | The registry owner the images are published under. | `diderisoft` |
| `BOT_BOTTLE_INFRA_ARTIFACT_USER` | release | The user the publish token belongs to (GHCR: the GitHub account). | `bot-bottle` |
| `BOT_BOTTLE_INFRA_EXT4_STORE` | release | Where the Firecracker rootfs is published: `oci` (an OCI artifact in the image registry) or `generic` (a Gitea generic package). | `oci` |
| `BOT_BOTTLE_IMAGE_SOURCE_URL` | release | The image's `org.opencontainers.image.source` label, which links the packages to the repository. | `https://github.com/diderisoft/bot-bottle` |
<!-- env-vars:settings:end -->

## Uninstall

```sh
bot-bottle uninstall            # everything bot-bottle set up; keeps ~/.bot-bottle
bot-bottle uninstall --purge    # also your agents, bottles, secrets, audit journal and frozen bottles
```

It shows what it will remove and asks first. It:

1. stops the host controller and each backend's orchestrator and gateway, and
   removes autostart;
2. removes each backend's containers, networks, volumes and images, and undoes
   each backend's privileged setup (on macOS it prints the `sudo` lines for
   the pf firewall; `--keep-backend` skips this);
3. removes `~/.cache/bot-bottle` and the package itself (the installer's venv
   and shim, or `pipx`/`uv tool uninstall`).

Base images it pulled to build on, such as `python` or `node`, stay.

If `bot-bottle` itself won't run, the installer can remove what it installed:

```sh
curl -fsSL "<the install URL>" | sh -s -- --uninstall
```

That removes the installer's venv and shim and says what's left.
