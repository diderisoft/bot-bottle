# Install

How bot-bottle gets onto a host, what each backend needs there, and how it
comes off again. The backends themselves are in [backends.md](backends.md).

## Installer

```sh
curl -fsSL "https://gitea.dideric.is/api/v1/repos/didericis/bot-bottle/raw/install.sh?ref=main" | sh
```

`install.sh` is a bootstrapper. It installs the latest release: the newest
`vX.Y.Z` tag, looked up through the forge's tags API (it fails, rather than
installing `main`, when it can't find one). It finds a suitable Python, installs
bot-bottle with its `remote` extra for phone pairing (none on Intel Macs,
which iroh has no build for) using `uv` or `pipx` if you have either, or a
private venv at `~/.bot-bottle/venv`, creates `~/.bot-bottle`, and runs
`bot-bottle setup` and `bot-bottle doctor`. It is idempotent and never uses
`sudo`.

Python-native users can skip it, pinning the latest tag (`v0.1.4` here; use
the newest one listed under the repository's tags):

```sh
uv tool install "bot-bottle[remote] @ git+https://gitea.dideric.is/didericis/bot-bottle.git@v0.1.4"
# or the same with: pipx install
```

To install `main` instead, pass `--main` (`… | sh -s -- --main`) or set
`BOT_BOTTLE_INSTALL_REF=main`, then build the infra images (below).

The installer's settings (`BOT_BOTTLE_PYTHON`, `BOT_BOTTLE_VENV`,
`BOT_BOTTLE_INSTALL_SPEC`, `BOT_BOTTLE_INSTALL_REF`, all optional) are in
[Settings](#settings).

### Releases and infra images

The normal path is a release: its orchestrator and gateway images are
published, and bot-bottle pulls and verifies them. Each release is tagged
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
`--build`.

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

| Backend | Needs |
|---|---|
| macos-container (macOS default) | Apple's `container` CLI, and the one-time host firewall. No Docker. |
| firecracker (Linux default with KVM) | `/dev/kvm`, the `firecracker` binary, e2fsprogs, and the one-time network pool. Docker only for `bot-bottle up --build` (infra images from source instead of a release's). |
| docker (fallback) | Docker |

#### macOS: Apple Container

`bot-bottle install --backend=macos-container` prints the `sudo` commands that
install the host firewall: a pf anchor and a LaunchDaemon that reloads it at
boot ([backends.md](backends.md#macos-container)). Without it, launches are
refused.

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
  `BOT_BOTTLE_FC_IP_BASE` if it clashes on your host.

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
| `BOT_BOTTLE_ROOT` | user | State root: manifests, keys, secrets, the host DB and audit journal. | `~/.bot-bottle` |
| `BOT_BOTTLE_LOG_LEVEL` | user | `debug`, `info`, `warn` or `error`. `debug` also turns on the orchestrator's access log. | `info` |
| `BOT_BOTTLE_INFRA_BUILD` | user | `local` builds the orchestrator and gateway images from source instead of pulling a release's, for every command; `bot-bottle up --build` is the flag form. Building is never automatic: an install with no published images that match (e.g. from `main`) stops without it. | pull |
| `BOT_BOTTLE_BUILD_ENGINE` | user | Image build engine: `docker`, `podman` or `vm`. | the first usable one |
| `BOT_BOTTLE_ARTIFACT_CACHE` | user | Where verified artifact downloads (infra rootfs, image layers) are cached. | `~/.cache/bot-bottle/artifacts` |
| `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN` | user | Token for pulling from, or with `write:package` publishing to, the private image registry. Removed at the GitHub cutover. | — |
| `BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS` | user | How long a bottle's teardown waits on the orchestrator. Read by the process that runs the launch (the host controller), so set it in that process's environment. | `30` |
| `BOT_BOTTLE_DOCKER_GATEWAY_SUBNET` | user | docker: the gateway network's subnet. | `10.242.255.0/24` |
| `BOT_BOTTLE_DOCKER_AGENT_SUBNETS` | user | docker: the range per-agent networks are carved from, as /29s. | `10.242.0.0/20` |
| `BOT_BOTTLE_MACOS_CONTAINER_DNS` | user | macos-container: DNS server for the gateway, orchestrator and image builder. When set, a builder running with other DNS is restarted with it. | the host's IPv4 resolver, else `1.1.1.1` |
| `BOT_BOTTLE_FC_CACHE` | user | firecracker: cache root for the kernel, root filesystems and run dirs. | `~/.cache/bot-bottle/firecracker` |
| `BOT_BOTTLE_FC_KERNEL` | user | firecracker: the guest kernel. | `vmlinux` in the firecracker cache |
| `BOT_BOTTLE_FC_DROPBEAR` | user | firecracker: the static dropbear put in every guest as its SSH server. | `dropbear` in the firecracker cache |
| `BOT_BOTTLE_FC_POOL_SIZE` | user | firecracker: TAP pool slots, so concurrent bottles. Must match the host's network pool. | `8` |
| `BOT_BOTTLE_FC_IP_BASE` | user | firecracker: base IPv4 of the pool's /31s. Must match the host's network pool. | `10.243.0.0` |
| `BOT_BOTTLE_FC_IFACE_PREFIX` | user | firecracker: agent TAP name prefix. Must match the host's network pool. | `bbfc` |
| `BOT_BOTTLE_FC_NFT_TABLE` | user | firecracker: the nftables isolation table. Must match the host's network pool. | `bot_bottle_fc` |
| `BOT_BOTTLE_FC_ORCH_IFACE` | user | firecracker: the orchestrator VM's TAP. Must match the host's network pool. | `bborch0` |
| `BOT_BOTTLE_FC_GW_IFACE` | user | firecracker: the gateway VM's TAP. Must match the host's network pool. | `bbgw0` |
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
| `BOT_BOTTLE_INFRA_ARTIFACT_BASE` | release | The registry the infra images are published to. Removed at the GitHub cutover. | `https://gitea.dideric.is` |
| `BOT_BOTTLE_INFRA_ARTIFACT_OWNER` | release | The registry owner the infra images are published under. Removed at the GitHub cutover. | `didericis` |
| `BOT_BOTTLE_IMAGE_SOURCE_URL` | release | The image's `org.opencontainers.image.source` label. Removed at the GitHub cutover. | `https://gitea.dideric.is/didericis/bot-bottle` |
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
