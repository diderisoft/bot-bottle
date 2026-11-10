# Install

One command installs bot-bottle and sets up the host. Each backend then needs
its own pieces, covered below, and one command takes it all off again. The
backends themselves are in [backends.md](backends.md).

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

- **Every host:** git and tmux 3.1+. Python 3.11+ if you have it. Otherwise
  the installer fetches one.
- **Linux:** KVM, the `firecracker` binary and e2fsprogs for the firecracker
  backend.
- **macOS:** an Apple silicon Mac with Apple's `container` CLI. Docker
  (`--backend=docker`) is the fallback anywhere else.

[Getting started](../start.md) walks through the first run.

## Installer

`install.sh` installs the latest release, sets up the host, and ends with the
one thing to do next.

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

It's a bootstrapper. In order, it:

1. finds the latest release: the newest `vX.Y.Z` tag, looked up through
   GitHub's tags API. It fails, rather than installing `main`, when it can't
   find one.
2. finds a suitable Python ([Python](#python)).
3. installs bot-bottle with its `remote` extra for phone pairing. It uses `uv`
   or `pipx` if you have either, or a private venv at `~/.bot-bottle/venv`.
4. creates `~/.bot-bottle`.
5. runs `bot-bottle bootstrap`, reading from your terminal under `curl | sh`.

Intel Macs get no `remote` extra, since iroh has no build for them.

`bot-bottle bootstrap` runs three commands:

| Command | Does |
|---|---|
| `install` | the backend's host setup, tmux, autostart |
| `setup` | starter agents, then an offer to pair your phone with a console. The default console is the hosted one at <https://bot-bottle.app>. Pairing is optional, and `--no-pair` skips it. |
| `doctor` | checks the host |

The installer is idempotent and never runs `sudo` on its own. The backend's one
root step is asked about first ([Host setup and sudo](#host-setup-and-sudo)).

It shows one line per step, with a spinner on a terminal. It keeps the package
tool's own output in `~/.bot-bottle/logs/install-<time>.log`, and prints its
end if a step fails.

With `BOT_BOTTLE_ENV` set, it says which environment it sets up. It ends with
doctor's `next:` line, the one thing to do next on this host.

### Upgrading with bottled agents

The installer refuses to upgrade while the installed version left any bottled
agent behind, running or kept for resume, in any environment. It checks first,
before anything is replaced.

Bottled agents now live in the host database, and nothing reads the files older
versions kept them in (`state/<id>/metadata.json`). One carried across the
upgrade couldn't be resumed or cleaned up. The refusal lists each one:

| Bottled agent | What to do |
|---|---|
| running | exit its agent, or run `bot-bottle cleanup bots` to stop them all |
| kept for resume | `bot-bottle resume <id>` and exit it, or delete its directory to discard it |

In a named environment, run those with `BOT_BOTTLE_ENV=<name>`. Then run the
installer again. Installing with `uv` or `pipx` yourself skips the check.

### Without the installer

Python-native users can skip it and pin the latest tag:

```sh
uv tool install "bot-bottle[remote] @ git+https://github.com/diderisoft/bot-bottle.git@v0.1.6"
# or the same with: pipx install
```

`v0.1.6` is an example. Use the newest one listed under the repository's tags.

### Installing main

To install `main` instead, pass `--main` (`… | sh -s -- --main`) or set
`BOT_BOTTLE_INSTALL_REF=main`. Then build the infra images
([below](#releases-and-infra-images)).

The installer's settings are in [Settings](#settings). They are
`BOT_BOTTLE_PYTHON`, `BOT_BOTTLE_VENV`, `BOT_BOTTLE_INSTALL_SPEC` and
`BOT_BOTTLE_INSTALL_REF`, all optional.

### Releases and infra images

A release pulls its published orchestrator and gateway images. An install from
`main` builds them instead:

```sh
bot-bottle up --build    # or set BOT_BOTTLE_INFRA_BUILD=local
```

The normal path is a release. Its orchestrator and gateway images are
published publicly on GHCR, and bot-bottle pulls and verifies them with no
registry login ([backends.md](backends.md#infra-images)).

Each release is tagged `vX.Y.Z` on the commit that pins its images, and both
install commands above install a tag. `main` matches the last release only
until a change to the orchestrator or gateway lands after it.

An install with no published images to match stops on its first bring-up and
says so. It never builds them on its own
([backends.md](backends.md#infra-images)).

So to run from `main`, build them locally with the command above. That's for
developing bot-bottle, or for a fix not yet released.

- **What it needs:** Docker on docker and firecracker hosts, and Apple's
  `container` on macOS.
- **How long it sticks:** launches after it keep building locally until an
  `up` without `--build`.
- **The exception:** that `up` keeps a running, current local build rather
  than stopping, as long as no release matches the install.

Agent images are separate:

- firecracker pulls the built-in ones as published root filesystems.
- It builds an agent's overlay with Docker or Podman when either is present,
  else with buildah inside the orchestrator VM.
- `BOT_BOTTLE_BUILD_ENGINE=docker|podman|vm` picks
  (`backend/container_engine.py`).
- A custom Dockerfile builds in the VM.

## Requirements

Every host needs Python 3.11 or newer (or none at all), git, tmux 3.1+, and one
backend.

### Python

**Python ≥ 3.11**, or nothing.

With no suitable Python, the installer has uv supply Python 3.13. It uses your
uv if you have one. Otherwise it downloads a pinned uv release and checks it
against its published SHA-256.

It keeps that uv privately under `~/.bot-bottle/uv`, with that Python and
bot-bottle itself. `bot-bottle uninstall` removes them.

#### macOS's own python3 is too old

When you do have Python, this is what trips people up on macOS. The `python3`
Apple ships at `/usr/bin/python3` is **3.9.6**, which is too old.

Bare `python3` resolves to that stub far more often than people expect.
`path_helper` builds a login shell's `PATH` from `/etc/paths` and then appends
`/etc/paths.d/*`. `/usr/bin` sits in the former.

So even when `/opt/homebrew/bin` *is* on the `PATH` (via
`/etc/paths.d/homebrew`), it comes after `/usr/bin` and loses.

Prepending a newer Python is something your shell profile does. A fresh
account, a launchd job, or a CI runner has no such profile.

So the installer looks past bare `python3` before giving up. It tries, in
order:

1. `python3`
2. the versioned `python3.11`–`python3.14` names
3. `/opt/homebrew/bin`, `/usr/local/bin` and `~/.local/bin`
4. python.org framework builds

It says which one it picked when it isn't the obvious one. Point it somewhere
specific with `BOT_BOTTLE_PYTHON=/path/to/python3`.

### No uv or pipx required

The installer uses `uv` or `pipx` when either is present, `uv` first, and stays
out of the way. With neither, it uses a private venv.

The venv is `~/.bot-bottle/venv`, and the installer symlinks the entry point
into `~/.local/bin`. Override the venv path with `BOT_BOTTLE_VENV`.

Debian and Ubuntu ship `venv` separately, as `python3-venv`. When the Python
has no `venv` module, the installer fetches its own pinned uv instead and
installs onto that Python with it. No `sudo apt install` is needed.

That's the same uv it uses when there's no Python at all, kept under
`~/.bot-bottle/uv`.

There is deliberately no `pip install --user` path. Homebrew, python.org and
Debian/Ubuntu interpreters are all externally managed (PEP 668), which blocks
`--user` outright. So on a Mac it is never the fallback it appears to be.

A venv is exempt from PEP 668, and `venv` is stdlib. Unlike `uv` or `pipx`,
there is nothing to bootstrap first.

### git and tmux

- **git**, because the default install spec is a `git+` URL. Set
  `BOT_BOTTLE_INSTALL_SPEC` to a wheel path or index name to avoid it.
- **tmux 3.1 or newer**, on every backend. It runs the agent sessions (see
  [host-controller.md](host-controller.md#sessions)).

### A backend

The installer deliberately does *not* install a backend. `bot-bottle doctor`
reports what's missing afterwards.

| Backend | Needs |
|---|---|
| macos-container (macOS default) | Apple's `container` CLI, and the one-time host firewall. No Docker. |
| firecracker (Linux default with KVM) | `/dev/kvm`, the `firecracker` binary, e2fsprogs, and the one-time network pool. Docker only for `bot-bottle up --build` (infra images from source instead of a release's). Docker or Podman, optionally, to build agent overlays on the host. |
| docker (fallback) | Docker |

Which backend a host gets is in [backends.md](backends.md#selection).

#### What doctor checks

`doctor` checks one backend in full: the one a launch here would use. That's
`BOT_BOTTLE_BACKEND`, else the one detected.

It shows that backend's diagnostics when it isn't ready, and that backend
decides doctor's exit code. Any other backend gets one `note:` line, ready or
not, and is never offered for install.

| Command | Does |
|---|---|
| `doctor --verbose` | shows every backend's diagnostics |
| `doctor --backend=X` | checks X alone |
| `doctor --next` | prints only the `next:` line |

`doctor` ends with `next:`, the most pressing of:

1. restart the host controller (`up --restart`)
2. set up the backend (`install --backend=…`)
3. write starters (`setup`)
4. start the services (`up`)
5. else run `bot-bottle`

#### Host setup and sudo

Each backend's one-time host setup has one step that needs root, and
bot-bottle asks before running it.

```
bot-bottle needs root once, to install the host firewall (a pf anchor, reloaded at boot).
It would run: sudo /Users/you/.local/bin/bot-bottle install --backend=macos-container
Run it now? [y/N]
```

The step is the macOS host firewall, or firecracker's network pool on a
systemd host. Run on a terminal, `bot-bottle install` says what that step is
and the exact command, and asks first. So do `bootstrap` and the installer.

- **Yes:** runs it. sudo asks for your password.
- **No, or no terminal:** prints the commands for you to run instead, as
  below.

It isn't asked once the step is in place. Nor is it asked on NixOS, where the
pool is a module you import. Nor on a Linux host without systemd, where it's a
script you run yourself.

#### macOS: Apple Container

`bot-bottle install --backend=macos-container` sets up the host firewall.
Without it, launches are refused.

```sh
bot-bottle install --backend=macos-container
```

It offers to install, or prints the `sudo` commands that install, the host
firewall. That's a pf anchor and a LaunchDaemon that reloads it at boot
([backends.md](backends.md#macos-container)).

Run as root through `sudo`, bot-bottle writes only those system pieces. It
doesn't register the environment or create its database, so no root-owned
state lands in your home. Your next ordinary run does that.

An older version may have left some root-owned state. Unprivileged commands
then fail on permissions. Every command but `doctor` stops and names the fix,
which `doctor` reports too:

```sh
sudo chown -R "$(id -un):$(id -gn)" <the paths it lists>
```

#### Linux: Firecracker

A KVM-capable Linux host defaults to the Firecracker backend.

```sh
BOT_BOTTLE_BACKEND=firecracker bot-bottle start <agent>
```

It needs:

- **`/dev/kvm`**, present and accessible.
- **`firecracker`** on `PATH`.
- **e2fsprogs** (`mke2fs`, `debugfs`, `e2fsck`, `resize2fs`) for building
  root filesystems.
- **A one-time privileged network setup:** the per-bottle TAP pool plus the
  fail-closed `nftables` isolation table.

**KVM.** Load `kvm-intel` or `kvm-amd`, and enable virtualization in
BIOS/firmware. The invoking user must be in the `kvm` group:
`sudo usermod -aG kvm "$USER"`, then log in again.

bot-bottle preflights this and reports exactly what's missing.

**firecracker.** Take a release from
<https://github.com/firecracker-microvm/firecracker/releases>. Start flows
print this pointer when the binary is missing.

**Docker is optional.** `bot-bottle up --build` needs it to build the infra
images from source rather than downloading a release's prebuilt ones. Docker
or Podman, when present, builds agent overlays on the host instead of in the
orchestrator VM.

**The network pool.** `bot-bottle install --backend=firecracker` gives the
host-appropriate config, plus autostart for the host controller. The config is
a NixOS module, or a `sudo` script elsewhere.

`bot-bottle doctor --backend=firecracker` reports what's present. That
includes whether the pool range collides with an existing route.

The pool defaults to `10.243.0.0/16`, an obscure RFC-1918 block. It dodges
Docker, libvirt, LANs and, deliberately, Tailscale's `100.64.0.0/10` CGNAT
range. Override it with `BOT_BOTTLE_FC_IP_BASE` if it clashes on your host.

The setup also reserves slices for named environments
([Environments](#environments)).

**NixOS.** For the network pool, use the flake module, then
`nixos-rebuild switch`. Imperative nft and TAP rules don't survive a rebuild.

```nix
imports = [ inputs.bot-bottle.nixosModules.firecracker-netpool ];
services.bot-bottle-firecracker = { enable = true; owner = "you"; };
```

Channel users can `imports = [ <bot-bottle>/nix/firecracker-netpool.nix ]`
instead.

Load the KVM module (`boot.kernelModules = [ "kvm-intel" ];` or `kvm-amd`) and
add your user to the `kvm` group.

Docker is only needed to build the infra images from source, with
`bot-bottle up --build`. That's `virtualisation.docker`, plus the `docker`
group.

`firecracker` isn't in nixpkgs as a user binary by default. Install the
release binary, pin the version, and put it on `PATH`.

#### Docker

Docker is the fallback for CI and hosts without Apple Container or KVM. Ask
for it explicitly:

```sh
BOT_BOTTLE_BACKEND=docker bot-bottle start <agent>   # or --backend=docker
```

It is the weakest boundary: agents share the host kernel unless gVisor is
registered. See [backends.md](backends.md#docker).

### Signing in an agent

A Claude agent signs in with your host's Claude Code login, or with a
long-lived token.

| Way | Manifest field | Notes |
|---|---|---|
| your host's Claude Code login | `agent_provider.login: host` | `bot-bottle setup` writes this when Claude Code is installed and logged in. |
| a long-lived token from `claude setup-token` | `agent_provider.login: {oauth_token: secret:NAME}`, naming the secret that holds it | |

Either way the gateway injects it, and the agent only ever sees a placeholder
([host-controller.md](host-controller.md#forwarded-host-logins)).

The manifest fields are in [manifest.md](manifest.md#agents).

## First run

`bot-bottle start <agent>` pulls the images on first run, then drops you into
the agent.

```sh
bot-bottle start <agent>   # pulls the images on first run, then drops you into the agent
```

From `main` rather than a release, run `bot-bottle up --build` first
([Releases and infra images](#releases-and-infra-images)).

## Uninstall

`bot-bottle uninstall` removes everything bot-bottle set up, after showing
what it will remove and asking first.

```sh
bot-bottle uninstall            # everything bot-bottle set up; keeps ~/.bot-bottle
bot-bottle uninstall --purge    # also your agents, bottles, secrets, audit journal and frozen bottles
```

It:

1. stops the host controller and each backend's orchestrator and gateway, and
   removes autostart.
2. removes each backend's containers, networks, volumes and images, and undoes
   each backend's privileged setup. On macOS it prints the `sudo` lines for
   the pf firewall. `--keep-backend` skips this step.
3. removes `~/.cache/bot-bottle` and the package itself (the installer's venv
   and shim, or `pipx`/`uv tool uninstall`).

Base images it pulled to build on, such as `python` or `node`, stay.

If `bot-bottle` itself won't run, the installer can remove what it installed:

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh -s -- --uninstall
```

That removes the installer's venv and shim and says what's left.

## Environments

*Advanced: for working on bot-bottle or running CI beside a live install.*

`BOT_BOTTLE_ENV` runs a separate install beside the live one, with its own
state, names and network.

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

Unset, or `live`, is the normal install. A name such as `dev` or `ci-6839` is a
separate environment (`bot_bottle/environment.py`). Working on bot-bottle or
running CI in one never touches the live install.

Names use lowercase letters, digits and `-`, at most 24.

Explicit settings still win. `BOT_BOTTLE_ROOT`, `BOT_BOTTLE_DOCKER_*` and
`BOT_BOTTLE_FC_*` override what the environment derives.

Image, artifact and root-filesystem caches are content-addressed and shared by
every environment. `live` derives exactly the names, paths and settings
fingerprint an install had before environments existed.

### Registering an environment

A named environment registers itself the first time a command runs in it, in
`~/.bot-bottle-envs/<name>/env.json`.

A name whose resource prefix would match another registered environment's is
refused. For example, `dev` and `dev-2` clash: `bb-dev-` matches
`bb-dev-2-orchestrator`.

`doctor` and `status` print the active environment and its orchestrator. They
warn about any other bot-bottle orchestrator answering.

An environment runs one backend's orchestrator and gateway at a time. Its
bring-up records which. Bringing up another backend's while that one answers
is refused ([architecture.md](architecture.md#components)).

### Removing an environment

```sh
bot-bottle envs                  # every environment, its root, and what runs
bot-bottle envs rm dev           # tear dev down and delete its state
bot-bottle envs rm dev --dry-run # what it would remove
```

`envs rm` runs the teardown as that environment. It removes:

- the bottles its host controller holds
- each available backend's leftover bottles, orchestrator, gateway,
  containers, networks and volumes, but not images
- its host controller and autostart unit

Then it deletes the state root and firecracker directory, and frees the pool
slice.

If part of the teardown fails, the state is kept so it can be retried.
`--force` deletes it anyway.

`envs rm` refuses `live`. Use `uninstall` for that. `uninstall` in turn
refuses to run in a named environment and removes only live's containers and
networks, so remove named environments first.

A root set with `BOT_BOTTLE_ROOT` outside `~/.bot-bottle-envs/` is left in
place.

### Firecracker slices

Each named environment claims a slice of the firecracker network pool that the
pool setup reserved up front, so it needs no root.

TAPs need root, so the network-pool setup reserves the live pool plus
`BOT_BOTTLE_FC_ENV_SLOTS` slices up front. The default is 4, or NixOS
`envSlots`. A named environment claims the lowest free one on first use.

Slice `k` (live is 0) has the live pool's size and:

| | live (`k = 0`) | slice `k` (1..63) |
|---|---|---|
| agent /31s | from `10.243.0.0` | from `10.243.<k>.0` (base + 256k) |
| agent TAPs | `bbfc0..` | `bb<k>fc0..` |
| nft tables | `bot_bottle_fc`, `_nat`, `_gw` | `bot_bottle_fc<k>`, `_nat`, `_gw` |
| orchestrator link | `bborch0`, host `10.243.255.0` | `bborch<k>`, host `10.243.255.<4k>` |
| gateway link | `bbgw0`, host `10.243.255.2` | `bbgw<k>`, host `10.243.255.<4k+2>` |

Each slice costs pool size + 2 TAPs. No slice's TAP wildcard (`bbfc*`,
`bb<k>fc*`) matches another's, so each slice's nft tables isolate only their
own VMs.

With every slice held, a named environment stops. It says to remove one
(`envs rm`), or to raise `BOT_BOTTLE_FC_ENV_SLOTS` and rerun the pool setup.

#### Slice claims

Claims are host-wide, so two users' environments never share a slice. A
developer's `dev` and a CI runner's `ci-<run>` are an example.

There's one file per claimed slice in `/var/lib/bot-bottle-fc/slices`. Override
the directory with `BOT_BOTTLE_FC_SLICES_DIR`, or NixOS `slicesDir`.

The pool setup creates that directory mode 2775 for the pool's group. The group
is `BOT_BOTTLE_FC_GROUP` or NixOS `group`, else the owner's primary group.
Every user that runs bot-bottle must be in that group.

A claim names its environment, user and registration file. It's created
atomically under a lock.

A claim is replaced only when stale: its registration is confirmed gone and
none of the slice's TAPs is in use. A live claim is never taken. `envs rm`
releases it (`backend/firecracker/slice_claims.py`).

### Docker and macOS subnets

A named environment allocates its docker gateway /24 from `10.241.0.0/16`, and
records it.

It tries the /24s in an order that starts at its name's hash. It skips any a
docker network already has. It moves on when docker reports a pool overlap,
meaning another environment got there first.

The one it gets is recorded in its host database (`infra_state`), so it's
stable across restarts. An existing gateway network's subnet is adopted.

The macOS agent IPv6 /64 is allocated the same way at setup, skipping the ones
other environments' anchors block. It's recorded in the installed pf rules.

### Ports

The docker orchestrator host port is the only host port bot-bottle publishes.
The docker gateway, macOS containers and firecracker VMs are reached on their
own networks, never through a host port, so they can't collide.

The host controller's loopback port is ephemeral and published under each
environment's own root.

Its guest listener keeps port 8098 in every environment, because it binds only
that environment's own link address. That's its orchestrator TAP, docker
publish bridge or vmnet control gateway.

The NixOS module opens 8098 on every slice's orchestrator TAP.

### macOS environments

Each environment has its own pf anchor, so a named environment needs its own
one-time install:

```sh
sudo env BOT_BOTTLE_ENV=<name> bot-bottle install --backend=macos-container
```

Setup skips agent subnets other environments' anchors already claim.

## Settings

Every `BOT_BOTTLE_*` variable you might set is below, all optional. Unset or
empty means the default.

The ones bot-bottle sets for its own processes are in
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
