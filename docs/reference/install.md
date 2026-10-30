# Install

How bot-bottle gets onto a host, what each backend needs there, and how it
comes off again. The backends themselves are in [backends.md](backends.md).

## Installer

```sh
curl -fsSL "https://gitea.dideric.is/api/v1/repos/didericis/bot-bottle/raw/install.sh?ref=main" | sh
```

`install.sh` is a bootstrapper. It finds a suitable Python, installs
bot-bottle with its `remote` extra for phone pairing (none on Intel Macs,
which iroh has no build for) using `uv` or `pipx` if you have either, or a
private venv at `~/.bot-bottle/venv`, creates `~/.bot-bottle`, and runs
`bot-bottle setup` and `bot-bottle doctor`. It is idempotent and never uses
`sudo`.

Python-native users can skip it:

```sh
uv tool install "bot-bottle[remote] @ git+https://gitea.dideric.is/didericis/bot-bottle.git"
# or the same with: pipx install
```

Installer settings, all optional:

| Variable | Does |
|---|---|
| `BOT_BOTTLE_PYTHON` | the interpreter to install with; skips the search |
| `BOT_BOTTLE_VENV` | where the venv install lives when there's no `uv` or `pipx` (default `~/.bot-bottle/venv`) |
| `BOT_BOTTLE_INSTALL_SPEC` | the pip spec to install instead of the default `git+` URL, such as a wheel path or index name |

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
| firecracker (Linux default with KVM) | `/dev/kvm`, the `firecracker` binary, e2fsprogs, and the one-time network pool. Docker only to build the infra images from source instead of using a release. |
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
  filesystems. Docker is optional: it is used only to build the infra images
  from source rather than downloading a release's prebuilt ones.
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
infra images from source. For the network pool, use the flake module, then
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
bot-bottle start <agent>   # pulls (or builds) the images on first run, then drops you into the agent
```

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
