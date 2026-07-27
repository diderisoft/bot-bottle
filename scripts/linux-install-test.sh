#!/usr/bin/env bash
# Clean-install test harness for the Linux path.
#
# Exercises install.sh the way a brand-new user would, inside a THROWAWAY
# QEMU/KVM virtual machine that is booted from a distro cloud image and
# deleted afterward. install.sh's entire footprint is user-home-local (the
# pipx venv under ~/.local, the ~/.bot-bottle config dir, and a printed PATH
# hint), so a fresh VM's fresh $HOME is the clean surface we want — and unlike
# a throwaway user account, tearing the VM down also wipes the OS-level
# prerequisites we install into it, so the reset is total. Full rationale in
# docs/research/testing-clean-install-on-linux.md.
#
# Why a VM and not a container or a throwaway user: a container shares the
# host kernel and cannot exercise a genuinely pristine OS (systemd, the distro
# package manager, PEP 668 externally-managed Python) the way a real guest
# does, and a throwaway user leaves every system package it installs behind.
# The host already needs KVM for the Firecracker backend, so a per-run,
# copy-on-write VM is cheap here: one cached base image, a throwaway overlay
# per run (`qemu-img create -b base`), deleted on teardown — the Linux
# equivalent of `docker run --rm`, but for a whole machine.
#
# What each run does: boot a fresh VM, install THIS distro's prerequisites
# (python3, git, pipx — see the DISTRO table), pipe *this checkout's*
# install.sh into it exactly as `curl … | sh` would, then assert the CLI
# installed cleanly and run `bot-bottle doctor` for visibility. doctor reports
# every backend as not-ready inside the VM (there is no nested KVM/Docker),
# which is expected: this harness gates INSTALLER correctness, not runtime, so
# the pass criterion is "install.sh exited 0, the bot-bottle entry point is on
# the new user's PATH, and `bot-bottle --version` runs" — NOT a green doctor.
#
# Usage:
#   ./scripts/linux-install-test.sh test            # up -> prereqs -> run -> status -> down (one distro)
#   ./scripts/linux-install-test.sh test-all        # `test` across every distro, with a summary
#   ./scripts/linux-install-test.sh up              # fetch base image, boot a fresh VM
#   ./scripts/linux-install-test.sh run             # install prereqs + install.sh + doctor in the VM
#   ./scripts/linux-install-test.sh status          # VM reachable? entry point installed? doctor
#   ./scripts/linux-install-test.sh down            # kill the VM, delete overlay + seed (the reset)
#   ./scripts/linux-install-test.sh ssh             # open an interactive shell in the running VM
#
# Config via env:
#   BB_TEST_DISTRO       ubuntu | fedora | arch | alpine | nixos   (default: ubuntu)
#   BB_TEST_SSH_PORT     host port forwarded to the guest's :22    (default: 2222)
#   BB_TEST_CACHE_DIR    where base images are cached              (default: ~/.cache/bot-bottle-install-test)
#   BB_TEST_RUN_DIR      per-run scratch (overlay, seed, key, pid) (default: a mktemp dir)
#   BB_TEST_MEM_MB       guest RAM                                 (default: 2048)
#   BB_TEST_CPUS         guest vCPUs                               (default: 2)
#   BB_TEST_DISK         overlay virtual size                     (default: 12G)
#   BB_TEST_BOOT_TIMEOUT seconds to wait for SSH after boot       (default: 300)
#   BB_TEST_KEEP         1 = `test`/`test-all` skip teardown, to poke at a failure
#   BB_TEST_INSTALL_URL  curl this install.sh in the guest instead of piping the local checkout
#   BB_TEST_SKIP_VERIFY  1 = skip base-image checksum verification (not recommended)
#   BOT_BOTTLE_INSTALL_SPEC   passed through to install.sh (pip / git spec)
#
# Notes:
#   * Needs /dev/kvm, qemu-system-x86_64, and cloud-localds (cloud-image-utils
#     / cloud-utils). On the NixOS host:  nix shell nixpkgs#qemu nixpkgs#cloud-utils
#   * Networking is user-mode (`-netdev user,hostfwd`) so the harness needs no
#     root, no bridge, and touches no host network state. Only SSH is forwarded.
#   * The cloud-image URLs in the DISTRO table are the one place to bump when a
#     distro cuts a new build; each is verified against the vendor's published
#     checksum at download time (guarding against truncated/corrupt pulls).

set -euo pipefail

DISTRO="${BB_TEST_DISTRO:-ubuntu}"
SSH_PORT="${BB_TEST_SSH_PORT:-2222}"
CACHE_DIR="${BB_TEST_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bot-bottle-install-test}"
MEM_MB="${BB_TEST_MEM_MB:-2048}"
CPUS="${BB_TEST_CPUS:-2}"
DISK="${BB_TEST_DISK:-12G}"
BOOT_TIMEOUT="${BB_TEST_BOOT_TIMEOUT:-300}"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"

# Per-run scratch. Persisted across sub-commands (up/run/status/down) via a
# marker file so `up` in one invocation and `down` in the next find the same
# VM; `test` creates its own and cleans it up itself.
RUN_DIR="${BB_TEST_RUN_DIR:-}"

# Set by `test`, which chains the steps and suppresses the per-step "next
# command" hints.
IN_TEST=0

# --- distro table ----------------------------------------------------
# For each distro:  cloud-image URL | checksum-file URL | default SSH user |
# prerequisite-install command (run in the guest; uses sudo when user != root).
#
# The prereq step is deliberate: the user asked that a clean install SUCCEED on
# every distro, so we install python3 + git + pipx first (install.sh installs
# none of them). That drives install.sh down its recommended pipx path on all
# five. Bump the URLs here when a distro publishes a newer build.
declare -A IMAGE_URL SUM_URL SSH_USER PREREQ

IMAGE_URL[ubuntu]="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
SUM_URL[ubuntu]="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
SSH_USER[ubuntu]="ubuntu"
PREREQ[ubuntu]="sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 git pipx"

IMAGE_URL[fedora]="https://download.fedoraproject.org/pub/fedora/linux/releases/41/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-41-1.4.x86_64.qcow2"
SUM_URL[fedora]="https://download.fedoraproject.org/pub/fedora/linux/releases/41/Cloud/x86_64/images/Fedora-Cloud-41-1.4-x86_64-CHECKSUM"
SSH_USER[fedora]="fedora"
PREREQ[fedora]="sudo dnf install -y python3 git pipx"

IMAGE_URL[arch]="https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"
SUM_URL[arch]="https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2.SHA256"
SSH_USER[arch]="arch"
PREREQ[arch]="sudo pacman -Sy --noconfirm python git python-pipx"

IMAGE_URL[alpine]="https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/cloud/generic_alpine-3.21.2-x86_64-bios-cloudinit-r0.qcow2"
SUM_URL[alpine]="https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/cloud/generic_alpine-3.21.2-x86_64-bios-cloudinit-r0.qcow2.sha256"
SSH_USER[alpine]="alpine"
PREREQ[alpine]="sudo apk add --no-cache python3 git pipx"

# NixOS is externally-managed in its own way (no FHS ~/.local on PATH by
# default, python/pipx not preinstalled). nix-env installs into the user
# profile — no sudo — and pipx then lays down a self-contained venv.
IMAGE_URL[nixos]="https://channels.nixos.org/nixos-24.11/latest-nixos-openstack-x86_64-linux.qcow2"
SUM_URL[nixos]=""   # channel image ships no stable sums file; set BB_TEST_SKIP_VERIFY=1
SSH_USER[nixos]="root"
PREREQ[nixos]="nix-env -iA nixos.python3 nixos.git nixos.pipx"

ALL_DISTROS=(ubuntu fedora arch alpine nixos)

# --- guards ----------------------------------------------------------
require_linux() {
    [ "$(uname -s)" = "Linux" ] \
        || { echo "error: this harness is Linux-only (uname is $(uname -s))" >&2; exit 1; }
}

require_kvm() {
    [ -e /dev/kvm ] && [ -r /dev/kvm ] && [ -w /dev/kvm ] \
        || { echo "error: /dev/kvm is missing or not accessible (add yourself to the 'kvm' group)" >&2; exit 1; }
}

require_tools() {
    local missing=()
    command -v qemu-system-x86_64 >/dev/null 2>&1 || missing+=(qemu-system-x86_64)
    command -v qemu-img          >/dev/null 2>&1 || missing+=(qemu-img)
    command -v cloud-localds     >/dev/null 2>&1 || missing+=(cloud-localds)
    command -v ssh               >/dev/null 2>&1 || missing+=(ssh)
    command -v curl              >/dev/null 2>&1 || missing+=(curl)
    if [ "${#missing[@]}" -ne 0 ]; then
        echo "error: missing required tools: ${missing[*]}" >&2
        echo "  on NixOS:  nix shell nixpkgs#qemu nixpkgs#cloud-utils nixpkgs#openssh nixpkgs#curl" >&2
        exit 1
    fi
}

known_distro() {
    [ -n "${IMAGE_URL[$DISTRO]:-}" ] \
        || { echo "error: unknown distro '$DISTRO' (known: ${ALL_DISTROS[*]})" >&2; exit 1; }
}

# --- run-dir bookkeeping ---------------------------------------------
# The marker lets `run`/`status`/`down` in separate invocations find the VM
# that `up` started. `test` sets RUN_DIR itself and never writes the marker.
_marker() { echo "${TMPDIR:-/tmp}/bot-bottle-install-test.$DISTRO.run"; }

_ensure_run_dir() {
    if [ -z "$RUN_DIR" ]; then
        RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bb-install-test.$DISTRO.XXXXXX")"
    fi
    mkdir -p "$RUN_DIR"
}

_load_run_dir() {
    if [ -z "$RUN_DIR" ] && [ -f "$(_marker)" ]; then
        RUN_DIR="$(cat "$(_marker)")"
    fi
    [ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ]
}

_ssh_key()  { echo "$RUN_DIR/id_ed25519"; }
_overlay()  { echo "$RUN_DIR/overlay.qcow2"; }
_seed()     { echo "$RUN_DIR/seed.iso"; }
_pidfile()  { echo "$RUN_DIR/qemu.pid"; }
_serial()   { echo "$RUN_DIR/serial.log"; }

# --- ssh helpers -----------------------------------------------------
_ssh_opts() {
    # No host-key pinning: the guest is thrown away every run.
    printf '%s\0' \
        -i "$(_ssh_key)" \
        -p "$SSH_PORT" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=8 \
        -o BatchMode=yes
}

guest() {
    local -a opts
    mapfile -d '' -t opts < <(_ssh_opts)
    ssh "${opts[@]}" "${SSH_USER[$DISTRO]}@127.0.0.1" "$@"
}

wait_for_ssh() {
    local deadline=$(( SECONDS + BOOT_TIMEOUT ))
    echo "== waiting for SSH on 127.0.0.1:$SSH_PORT (up to ${BOOT_TIMEOUT}s) =="
    while [ "$SECONDS" -lt "$deadline" ]; do
        if guest true 2>/dev/null; then
            echo "   guest is up"
            return 0
        fi
        # Bail early if QEMU has died — no point waiting out the timeout.
        if [ -f "$(_pidfile)" ] && ! kill -0 "$(cat "$(_pidfile)")" 2>/dev/null; then
            echo "error: QEMU exited before SSH came up; see $(_serial)" >&2
            return 1
        fi
        sleep 3
    done
    echo "error: timed out waiting for SSH; see $(_serial)" >&2
    return 1
}

# --- image cache -----------------------------------------------------
_base_image() {
    # One cached file per distro, keyed by the image's basename so a URL bump
    # to a newer build lands as a new cache entry rather than a stale hit.
    local url="${IMAGE_URL[$DISTRO]}"
    echo "$CACHE_DIR/$DISTRO-$(basename "$url")"
}

verify_checksum() {
    local file="$1" sums_url="${SUM_URL[$DISTRO]}" base
    base="$(basename "${IMAGE_URL[$DISTRO]}")"
    if [ "${BB_TEST_SKIP_VERIFY:-0}" = "1" ] || [ -z "$sums_url" ]; then
        echo "   checksum: SKIPPED (${sums_url:+set BB_TEST_SKIP_VERIFY=0 to enable}${sums_url:-no sums URL for $DISTRO})" >&2
        return 0
    fi
    local want
    # Vendors publish either "HASH  filename" tables or a bare "HASH" (or a
    # "SHA256 (file) = HASH" BSD line, e.g. Fedora). Cover all three.
    local sums; sums="$(curl -fsSL "$sums_url")"
    want="$(printf '%s\n' "$sums" | awk -v f="$base" '
        $0 ~ f && $1 ~ /^[0-9a-fA-F]{64}$/ { print $1; exit }        # GNU "hash  file"
        $1=="SHA256" && $0 ~ f { gsub(/[()]/,""); print $NF; exit }  # BSD "SHA256 (file) = hash"
    ')"
    [ -z "$want" ] && want="$(printf '%s\n' "$sums" | awk '/^[0-9a-fA-F]{64}$/ {print $1; exit}')"
    [ -n "$want" ] || { echo "error: could not find a sha256 for $base in $sums_url" >&2; return 1; }
    local got; got="$(sha256sum "$file" | awk '{print $1}')"
    if [ "$want" != "$got" ]; then
        echo "error: checksum mismatch for $base" >&2
        echo "  want $want" >&2
        echo "  got  $got" >&2
        return 1
    fi
    echo "   checksum: OK"
}

ensure_base_image() {
    mkdir -p "$CACHE_DIR"
    local base; base="$(_base_image)"
    if [ -f "$base" ]; then
        echo "== base image cached: $base =="
        return 0
    fi
    echo "== downloading $DISTRO cloud image =="
    echo "   ${IMAGE_URL[$DISTRO]}"
    # Download to a temp name and rename on success so an interrupted pull
    # never poisons the cache with a truncated image.
    local tmp="$base.partial"
    curl -fSL --retry 3 -o "$tmp" "${IMAGE_URL[$DISTRO]}"
    verify_checksum "$tmp"
    mv "$tmp" "$base"
    echo "   cached: $base"
}

# --- cloud-init seed -------------------------------------------------
make_seed() {
    ssh-keygen -t ed25519 -N '' -f "$(_ssh_key)" -q
    local pub; pub="$(cat "$(_ssh_key).pub")"
    local user="${SSH_USER[$DISTRO]}"
    local user_data="$RUN_DIR/user-data"

    if [ "$user" = "root" ]; then
        # NixOS' cloud-init lands the key straight on root; no sudo needed.
        cat > "$user_data" <<EOF
#cloud-config
ssh_authorized_keys:
  - $pub
EOF
    else
        cat > "$user_data" <<EOF
#cloud-config
users:
  - name: $user
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/sh
    lock_passwd: true
    ssh_authorized_keys:
      - $pub
EOF
    fi
    # meta-data can be empty but must exist for NoCloud.
    : > "$RUN_DIR/meta-data"
    cloud-localds "$(_seed)" "$user_data" "$RUN_DIR/meta-data"
}

# --- commands --------------------------------------------------------
cmd_up() {
    require_linux; require_kvm; require_tools; known_distro
    _ensure_run_dir
    ensure_base_image

    # Throwaway copy-on-write overlay: the cached base is read-only backing,
    # all guest writes land in the overlay, and `down` deletes it. Resize so
    # pipx + a git build have headroom (cloud-init grows the rootfs to fit).
    qemu-img create -q -f qcow2 -F qcow2 -b "$(_base_image)" "$(_overlay)" "$DISK"
    make_seed

    echo "== booting $DISTRO VM (mem=${MEM_MB}M cpus=$CPUS, ssh -> :$SSH_PORT) =="
    qemu-system-x86_64 \
        -machine accel=kvm -cpu host -smp "$CPUS" -m "$MEM_MB" \
        -display none -daemonize \
        -pidfile "$(_pidfile)" \
        -serial "file:$(_serial)" \
        -drive "file=$(_overlay),if=virtio,format=qcow2" \
        -drive "file=$(_seed),if=virtio,format=raw" \
        -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" \
        -device virtio-net-pci,netdev=n0

    # Only publish the marker (so a later `run`/`down` finds this VM) when we
    # aren't inside `test`, which manages its own RUN_DIR + teardown.
    [ "$IN_TEST" = 1 ] || echo "$RUN_DIR" > "$(_marker)"

    wait_for_ssh
    if [ "$IN_TEST" != 1 ]; then
        echo "== VM is up. Install into it with: BB_TEST_DISTRO=$DISTRO $0 run =="
    fi
}

cmd_run() {
    require_linux
    _load_run_dir || { echo "error: no running VM for $DISTRO; run '$0 up' first" >&2; return 1; }

    echo "== [prereqs] installing python3 + git + pipx on $DISTRO =="
    # Runs via the guest's login shell; PREREQ is a client-side table value.
    guest "${PREREQ[$DISTRO]}"

    local spec_env=""
    [ -n "${BOT_BOTTLE_INSTALL_SPEC:-}" ] \
        && spec_env="BOT_BOTTLE_INSTALL_SPEC='$BOT_BOTTLE_INSTALL_SPEC' "

    echo "== installing bot-bottle as ${SSH_USER[$DISTRO]} =="
    if [ -n "${BB_TEST_INSTALL_URL:-}" ]; then
        guest "curl -fsSL '$BB_TEST_INSTALL_URL' | ${spec_env}sh"
    else
        # Feed THIS checkout's install.sh in over stdin — the same `curl … | sh`
        # shape a real user runs, and nothing is staged in the guest to leak.
        guest "${spec_env}sh -s" < "$_REPO_ROOT/install.sh"
    fi
    [ "$IN_TEST" = 1 ] \
        || echo "== re-check anytime with: BB_TEST_DISTRO=$DISTRO $0 status =="
}

# `bot-bottle doctor` in the guest, for visibility. install.sh prints a PATH
# hint rather than editing a profile, so on a fresh login the entry point may
# be installed but not yet on PATH — try the known pipx/pip locations too.
doctor_in_guest() {
    # shellcheck disable=SC2016  # $HOME must expand in the GUEST shell.
    guest '
        for bb in "$HOME/.local/bin/bot-bottle" "$HOME/.bot-bottle/venv/bin/bot-bottle"; do
            if [ -x "$bb" ]; then
                command -v bot-bottle >/dev/null 2>&1 \
                    || echo "  (not on PATH — running $bb directly, as install.sh advises)"
                exec "$bb" doctor
            fi
        done
        command -v bot-bottle >/dev/null 2>&1 && exec bot-bottle doctor
        echo "  no bot-bottle entry point found for this user" >&2
        exit 1
    '
}

# The teeth of the harness. PASS requires the entry point to exist and run;
# it does NOT require a green doctor, because no backend can be ready inside
# the VM (no nested KVM/Docker) and doctor exits non-zero in that state by
# design. We print doctor's output regardless so a real installer regression
# (a Python/import break, a broken shim) is visible.
assert_installed() {
    # shellcheck disable=SC2016  # expand in the GUEST shell.
    if ! guest '
        for bb in "$HOME/.local/bin/bot-bottle" "$HOME/.bot-bottle/venv/bin/bot-bottle" "$(command -v bot-bottle 2>/dev/null)"; do
            [ -n "$bb" ] && [ -x "$bb" ] || continue
            "$bb" --version >/dev/null 2>&1 && exit 0
            echo "  found $bb but \"$bb --version\" failed" >&2
            exit 1
        done
        echo "  no runnable bot-bottle entry point for this user" >&2
        exit 1
    '; then
        echo "FAIL[$DISTRO]: bot-bottle did not install into a runnable state" >&2
        return 1
    fi
    echo "OK[$DISTRO]: bot-bottle entry point installed and runnable"
}

cmd_status() {
    require_linux
    if ! _load_run_dir; then
        echo "vm:      no running VM for $DISTRO"
        return 0
    fi
    if [ -f "$(_pidfile)" ] && kill -0 "$(cat "$(_pidfile)")" 2>/dev/null; then
        echo "vm:      $DISTRO running (pid $(cat "$(_pidfile)"), ssh :$SSH_PORT)"
    else
        echo "vm:      $DISTRO run-dir present but QEMU not alive"
        return 1
    fi
    local rc=0
    echo "doctor (in guest):"
    doctor_in_guest || rc=1   # advisory: backend not-ready inside the VM is expected
    assert_installed || return 1
    return 0
}

cmd_down() {
    require_linux
    if ! _load_run_dir; then
        echo "$DISTRO: nothing running"
        return 0
    fi
    if [ -f "$(_pidfile)" ]; then
        local pid; pid="$(cat "$(_pidfile)")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    # Deleting the overlay + seed is the reset; the read-only base stays cached.
    rm -f "$(_overlay)" "$(_seed)" "$(_ssh_key)" "$(_ssh_key).pub" \
          "$RUN_DIR/user-data" "$RUN_DIR/meta-data"
    # Only remove a scratch dir we created (leave a user-provided one alone).
    [ -n "${BB_TEST_RUN_DIR:-}" ] || rmdir "$RUN_DIR" 2>/dev/null || true
    rm -f "$(_marker)"
    echo "removed $DISTRO VM and its overlay — install surface is clean."
}

cmd_ssh() {
    require_linux
    _load_run_dir || { echo "error: no running VM for $DISTRO" >&2; return 1; }
    local -a opts
    mapfile -d '' -t opts < <(_ssh_opts)
    exec ssh -t "${opts[@]}" "${SSH_USER[$DISTRO]}@127.0.0.1"
}

# Teardown half of `test`, armed the moment the VM exists so a failure or a
# Ctrl-C still leaves nothing running.
_test_teardown() {
    local rc=$?
    trap - EXIT INT TERM
    if [ "${BB_TEST_KEEP:-0}" = "1" ]; then
        echo
        echo "== down: SKIPPED (BB_TEST_KEEP=1); VM still up, remove with: BB_TEST_DISTRO=$DISTRO $0 down =="
        echo "   (ssh in with: BB_TEST_RUN_DIR=$RUN_DIR BB_TEST_DISTRO=$DISTRO $0 ssh)"
        exit "$rc"
    fi
    echo
    echo "== down =="
    cmd_down || rc=1
    exit "$rc"
}

cmd_test() {
    require_linux; require_kvm; require_tools; known_distro
    IN_TEST=1
    RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bb-install-test.$DISTRO.XXXXXX")"

    echo "== [1/4] up ($DISTRO) =="
    cmd_up
    trap _test_teardown EXIT INT TERM

    echo
    echo "== [2/4] run =="
    cmd_run

    echo
    echo "== [3/4] status =="
    local rc=0
    echo "doctor (in guest):"
    doctor_in_guest || true   # advisory only
    assert_installed || rc=1

    if [ "$rc" -eq 0 ]; then
        echo
        echo "PASS[$DISTRO]: a brand-new user can install bot-bottle."
    else
        echo
        echo "FAIL[$DISTRO]: see above (the VM is torn down regardless)." >&2
    fi
    # _test_teardown runs on exit and owns the final exit code.
    return "$rc"
}

cmd_test_all() {
    require_linux; require_kvm; require_tools
    local -a passed=() failed=()
    local port="$SSH_PORT"
    for d in "${ALL_DISTROS[@]}"; do
        echo
        echo "########################################################"
        echo "# $d"
        echo "########################################################"
        # Each distro gets its own forwarded port so a leftover from a prior
        # distro can't collide, and its own subshell so a `test` failure (or a
        # teardown trap) doesn't abort the whole matrix.
        if ( DISTRO="$d" SSH_PORT="$port" BB_TEST_DISTRO="$d" BB_TEST_SSH_PORT="$port" \
                bash "$_SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")" test ); then
            passed+=("$d")
        else
            failed+=("$d")
        fi
        port=$(( port + 1 ))
    done
    echo
    echo "== matrix summary =="
    echo "  PASS: ${passed[*]:-(none)}"
    echo "  FAIL: ${failed[*]:-(none)}"
    [ "${#failed[@]}" -eq 0 ]
}

case "${1:-}" in
    test)     cmd_test ;;
    test-all) cmd_test_all ;;
    up)       cmd_up ;;
    run)      cmd_run ;;
    status)   cmd_status ;;
    down)     cmd_down ;;
    ssh)      cmd_ssh ;;
    *) echo "usage: $0 {test|test-all|up|run|status|down|ssh}  (distro via BB_TEST_DISTRO)" >&2; exit 2 ;;
esac
