#!/usr/bin/env bash
# Clean-install test harness for the Linux path.
#
# Exercises install.sh the way a brand-new user would, inside a THROWAWAY
# QEMU/KVM virtual machine that is booted from a distro cloud image and
# deleted afterward. install.sh's entire footprint is user-home-local (the
# pipx venv under ~/.local, the ~/.bot-bottle config dir, and a printed PATH
# hint), so a fresh VM's fresh $HOME is the clean surface we want — and unlike
# a throwaway user account, tearing the VM down also wipes any OS-level
# prerequisites installed into it, so the reset is total. Full rationale in
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
# Usage:
#   ./scripts/linux-install-test.sh test        # up -> run -> verdict -> down (bare host)
#   ./scripts/linux-install-test.sh test-ready  # ... with prerequisites installed first
#   ./scripts/linux-install-test.sh test-all    # every distro × both variants, with a summary
#   ./scripts/linux-install-test.sh up          # fetch base image, boot a fresh VM
#   ./scripts/linux-install-test.sh prereqs     # install python3 + git + pipx in the VM
#   ./scripts/linux-install-test.sh run         # pipe install.sh into the VM
#   ./scripts/linux-install-test.sh status      # VM reachable? is the install sound?
#   ./scripts/linux-install-test.sh down        # kill the VM, delete overlay + seed (the reset)
#   ./scripts/linux-install-test.sh ssh         # open an interactive shell in the running VM
#
# TWO TEST VARIANTS, because "does install.sh handle an unprepared host" and
# "does a prepared host get a clean install" are different questions (mirrors
# the macOS harness's test / test-ready split):
#
#   test        A Linux system WITHOUT the prerequisites set up — the default
#               state of a stock cloud image (python3 is usually present for
#               cloud-init, but git and pipx are not). This exercises
#               install.sh's own prerequisite-guard logic. It is SOUND — a
#               PASS — when install.sh EITHER installs cleanly (the image
#               already had enough) OR declines with one of its own recognized,
#               actionable errors (missing python3/git, no usable pip, PEP 668
#               externally-managed). A crash or an unrecognized failure fails.
#
#   test-ready  A Linux system WITH the prerequisites satisfied — the harness
#               installs python3 + git + pipx first (see the DISTRO table),
#               then runs install.sh. A graceful decline is no longer good
#               enough here: the install MUST land, the entry point must run,
#               and doctor must report a usable python and config without
#               crashing.
#
# Neither variant requires a green backend. install.sh does not install a
# backend and cannot regress one, and inside a plain VM there is no nested KVM
# for Firecracker; the harness does not provision the Docker backend either. So
# backend readiness is reported, not required (this is where Linux necessarily
# diverges from the macOS test-ready, which reaches the host backend).
# BB_TEST_REQUIRE_BACKEND=1 makes it fatal anyway, for a nested-virt host.
#
# Config via env:
#   BB_TEST_DISTRO         ubuntu | fedora | arch | alpine | nixos (default: ubuntu)
#   BB_TEST_SSH_PORT       host port forwarded to the guest's :22  (default: 2222)
#   BB_TEST_CACHE_DIR      where base images are cached            (default: ~/.cache/bot-bottle-install-test)
#   BB_TEST_RUN_DIR        per-run scratch (overlay, seed, key, …) (default: a mktemp dir)
#   BB_TEST_MEM_MB         guest RAM                               (default: 2048)
#   BB_TEST_CPUS           guest vCPUs                             (default: 2)
#   BB_TEST_DISK           overlay virtual size                   (default: 12G)
#   BB_TEST_BOOT_TIMEOUT   seconds to wait for SSH after boot      (default: 300)
#   BB_TEST_KEEP           1 = the test cycles skip teardown, to poke at a failure
#   BB_TEST_REQUIRE_BACKEND 1 = make a not-ready backend fatal (needs nested virt)
#   BB_TEST_INSTALL_URL    curl this install.sh in the guest instead of piping the local checkout
#   BB_TEST_SKIP_VERIFY    1 = skip base-image checksum verification (not recommended)
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

# Per-run scratch. Persisted across sub-commands (up/prereqs/run/status/down)
# via a marker file so `up` in one invocation and `down` in the next find the
# same VM; the test cycles set RUN_DIR themselves and never write the marker.
RUN_DIR="${BB_TEST_RUN_DIR:-}"

# Set by the test cycles, which chain the steps and suppress the per-step "next
# command" hints. _STEPS / _PASS_CLAIM / _REQUIRE_INSTALL are the per-variant
# knobs the shared cycle and teardown read.
IN_TEST=0
_STEPS=4
_PASS_CLAIM=""
_REQUIRE_INSTALL=0

# --- distro table ----------------------------------------------------
# For each distro:  cloud-image URL | checksum-file URL | default SSH user |
# prerequisite-install command (run in the guest; uses sudo when user != root).
#
# The prerequisite command installs python3 + git + pipx — install.sh installs
# none of them — so `test-ready` (and the `prereqs` sub-command) drive
# install.sh down its recommended pipx path. Bump the URLs here when a distro
# publishes a newer build.
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
# The marker lets prereqs/run/status/down in separate invocations find the VM
# that `up` started. The test cycles set RUN_DIR themselves and never write it.
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

    # Only publish the marker (so a later prereqs/run/down finds this VM) when
    # we aren't inside a test cycle, which manages its own RUN_DIR + teardown.
    [ "$IN_TEST" = 1 ] || echo "$RUN_DIR" > "$(_marker)"

    wait_for_ssh
    if [ "$IN_TEST" != 1 ]; then
        echo "== VM is up. Prepare it with: BB_TEST_DISTRO=$DISTRO $0 prereqs (or go straight to 'run') =="
    fi
}

# Install install.sh's toolchain prerequisites (python3 + git + pipx) into the
# running VM. This is what separates `test-ready` from `test`, and it is a
# distinct sub-command so a manual up/prereqs/run/down cycle is possible.
cmd_prereqs() {
    require_linux
    _load_run_dir || { echo "error: no running VM for $DISTRO; run '$0 up' first" >&2; return 1; }
    echo "== installing prerequisites (python3 + git + pipx) on $DISTRO =="
    # Runs via the guest login shell; PREREQ is a client-side table value.
    guest "${PREREQ[$DISTRO]}"
}

cmd_run() {
    require_linux
    _load_run_dir || { echo "error: no running VM for $DISTRO; run '$0 up' first" >&2; return 1; }

    local spec_env=""
    [ -n "${BOT_BOTTLE_INSTALL_SPEC:-}" ] \
        && spec_env="BOT_BOTTLE_INSTALL_SPEC='$BOT_BOTTLE_INSTALL_SPEC' "

    echo "== installing bot-bottle as ${SSH_USER[$DISTRO]} =="
    # Capture install.sh's exit code and full output rather than aborting on
    # non-zero: on a bare host a clean prerequisite *decline* is sound, so the
    # verdict step — not set -e — decides the outcome.
    local rc
    set +e
    if [ -n "${BB_TEST_INSTALL_URL:-}" ]; then
        guest "curl -fsSL '$BB_TEST_INSTALL_URL' | ${spec_env}sh" 2>&1 | tee "$RUN_DIR/install.log"
    else
        # Feed THIS checkout's install.sh in over stdin — the same `curl … | sh`
        # shape a real user runs, and nothing is staged in the guest to leak.
        guest "${spec_env}sh -s" < "$_REPO_ROOT/install.sh" 2>&1 | tee "$RUN_DIR/install.log"
    fi
    rc="${PIPESTATUS[0]}"
    set -e
    printf '%s\n' "$rc" > "$RUN_DIR/install.rc"
    echo "== install.sh exited $rc =="
    [ "$IN_TEST" = 1 ] \
        || echo "== verdict anytime with: BB_TEST_DISTRO=$DISTRO $0 status =="
}

# Quietly report whether a runnable bot-bottle entry point exists for the
# guest user, checking the pipx/pip locations install.sh may leave off PATH.
entry_point_runnable() {
    # shellcheck disable=SC2016  # expand in the GUEST shell.
    guest '
        for bb in "$HOME/.local/bin/bot-bottle" "$HOME/.bot-bottle/venv/bin/bot-bottle" "$(command -v bot-bottle 2>/dev/null)"; do
            [ -n "$bb" ] && [ -x "$bb" ] || continue
            "$bb" --version >/dev/null 2>&1 && exit 0
        done
        exit 1
    ' >/dev/null 2>&1
}

# `bot-bottle doctor` in the guest, classified. doctor's own exit code
# conflates "is the install sound" with "is a backend ready to run a bottle" —
# and inside a plain VM no backend can be ready (no nested KVM/Docker), so the
# raw exit code is non-zero by design. This separates the two: an unhandled
# traceback, or a missing python/config line, is an install defect and fails;
# a not-ready backend is reported, not fatal (unless BB_TEST_REQUIRE_BACKEND=1,
# for a nested-virt host that can actually satisfy it).
doctor_in_guest() {
    local out rc=0 bad=0
    out="$(mktemp "${TMPDIR:-/tmp}/bb-doctor.XXXXXX")"
    # shellcheck disable=SC2016  # $HOME/$bb must expand in the GUEST shell.
    guest '
        for bb in bot-bottle "$HOME/.local/bin/bot-bottle" "$HOME/.bot-bottle/venv/bin/bot-bottle"; do
            if command -v "$bb" >/dev/null 2>&1; then
                case "$bb" in
                    bot-bottle) : ;;
                    *) echo "  (not on PATH — running $bb directly, as install.sh advises)" ;;
                esac
                exec "$bb" doctor
            fi
        done
        echo "  no bot-bottle entry point found for this user" >&2
        exit 1
    ' >"$out" 2>&1 || rc=$?
    cat "$out"

    # An unhandled exception is always an install/product defect, never an
    # environment fact — doctor's non-zero exit alone would not distinguish it.
    if grep -q 'Traceback (most recent call last)' "$out"; then
        echo "  doctor crashed (traceback above) — a defect, not a missing prerequisite" >&2
        bad=1
    fi
    grep -qE '^ok: +python:' "$out" \
        || { echo "  doctor never reported a usable python" >&2; bad=1; }
    grep -qE '^ok: +config:' "$out" \
        || { echo "  doctor never reported a usable config dir" >&2; bad=1; }
    if [ "$rc" -ne 0 ] && ! grep -qE '^(fail|warn): +backend' "$out"; then
        echo "  doctor failed for something other than backend readiness" >&2
        bad=1
    fi

    local backend_ready=1
    grep -qE '^fail: +backend' "$out" && backend_ready=0
    rm -f "$out"

    [ "$bad" -eq 0 ] || return 1
    if [ "${BB_TEST_REQUIRE_BACKEND:-0}" = "1" ] && [ "$backend_ready" -eq 0 ]; then
        echo "  backend is not ready and BB_TEST_REQUIRE_BACKEND=1 — failing" >&2
        return 1
    fi
    if [ "$backend_ready" -eq 1 ]; then
        echo "  doctor: install sound; a backend is ready"
    else
        echo "  doctor: install sound; no backend ready (expected in a plain VM — install gate only)"
    fi
    return 0
}

# The prerequisite-decline messages install.sh prints via die(). On a bare host
# ANY of these means install.sh correctly refused rather than half-installing —
# a sound outcome for `test`.
PREREQ_ERR_RE='is required but was not found|or newer is required|git is required to install from|neither pipx nor a usable|externally managed \(PEP 668\)|is not on PATH'

# The verdict: is the install sound? Reads install.sh's captured exit code and
# output (from cmd_run) plus the guest's resulting state.
#   - installed & runnable  -> the verdict is doctor's soundness classification.
#   - no entry point, but a recognized prerequisite decline, and declines are
#     allowed (bare `test`, _REQUIRE_INSTALL=0) -> sound.
#   - anything else -> not sound.
install_verdict() {
    local rc=""
    [ -f "$RUN_DIR/install.rc" ] && rc="$(cat "$RUN_DIR/install.rc")"

    if [ "${rc:-1}" = 0 ] && entry_point_runnable; then
        echo "doctor (in guest):"
        doctor_in_guest
        return $?
    fi

    if [ "${_REQUIRE_INSTALL:-0}" != "1" ] \
            && [ -n "$rc" ] && [ "$rc" != 0 ] \
            && [ -f "$RUN_DIR/install.log" ] \
            && grep -Eiq "$PREREQ_ERR_RE" "$RUN_DIR/install.log"; then
        echo "  install.sh declined with an actionable prerequisite error (rc=$rc)"
        echo "  — sound on a bare host; run 'test-ready' (or 'prereqs') to install."
        return 0
    fi

    if [ "${_REQUIRE_INSTALL:-0}" = "1" ]; then
        echo "  prerequisites were provisioned, but install.sh left no runnable entry point (rc=${rc:-?})" >&2
    else
        echo "  install.sh neither installed nor gave a recognized prerequisite error (rc=${rc:-?})" >&2
    fi
    return 1
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
    if install_verdict; then
        echo "OK[$DISTRO]: the install is sound"
        return 0
    fi
    echo "FAIL[$DISTRO]: the install is not sound (see above)" >&2
    return 1
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
          "$RUN_DIR/user-data" "$RUN_DIR/meta-data" \
          "$RUN_DIR/install.rc" "$RUN_DIR/install.log"
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

# Teardown half of the test cycles, armed the moment the VM exists so a failure
# or a Ctrl-C still leaves nothing running.
_test_teardown() {
    local rc=$?
    trap - EXIT INT TERM
    if [ "${BB_TEST_KEEP:-0}" = "1" ]; then
        echo
        echo "== [$_STEPS/$_STEPS] down: SKIPPED (BB_TEST_KEEP=1) =="
        echo "   VM still up; remove with: BB_TEST_DISTRO=$DISTRO $0 down"
        echo "   ssh in with: BB_TEST_RUN_DIR=$RUN_DIR BB_TEST_DISTRO=$DISTRO $0 ssh"
        exit "$rc"
    fi
    echo
    echo "== [$_STEPS/$_STEPS] down =="
    cmd_down || rc=1
    if [ "$rc" -eq 0 ]; then
        echo
        echo "PASS[$DISTRO]: $_PASS_CLAIM"
    else
        echo
        echo "FAIL[$DISTRO]: see above (the VM was torn down regardless)." >&2
    fi
    exit "$rc"
}

# The two variants differ only in whether the prerequisites get installed
# before install.sh runs, which is exactly the question each one asks:
#
#   test        a bare host — assert install.sh is SOUND (installs cleanly, or
#               declines with an actionable prerequisite error).
#   test-ready  prerequisites satisfied — assert install.sh actually LANDS.
_test_cycle() {
    local with_prereqs="$1"
    require_linux; require_kvm; require_tools; known_distro
    IN_TEST=1
    RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bb-install-test.$DISTRO.XXXXXX")"

    echo "== [1/$_STEPS] up ($DISTRO) =="
    cmd_up
    trap _test_teardown EXIT INT TERM

    local step=2
    if [ "$with_prereqs" = 1 ]; then
        echo
        echo "== [$step/$_STEPS] prereqs =="
        cmd_prereqs || { echo "error: could not install prerequisites (see above)." >&2; return 1; }
        step=$(( step + 1 ))
    fi

    echo
    echo "== [$step/$_STEPS] run =="
    cmd_run
    step=$(( step + 1 ))

    echo
    echo "== [$step/$_STEPS] verdict =="
    # install.sh exits 0 even when doctor reports unmet prerequisites, so the
    # install succeeding is not the verdict — this is.
    cmd_status || {
        echo "error: the install is not sound for $DISTRO (see above)." >&2
        echo "  re-run with BB_TEST_KEEP=1 to keep the VM and dig in." >&2
        return 1
    }
}

cmd_test() {
    _STEPS=4
    _PASS_CLAIM="on a bare $DISTRO host, install.sh behaves soundly."
    _test_cycle 0
}

cmd_test_ready() {
    _STEPS=5
    _PASS_CLAIM="a $DISTRO host with prerequisites satisfied installs bot-bottle cleanly."
    # Prerequisites are provisioned, so a graceful decline is no longer an
    # acceptable outcome — the install must actually land.
    _REQUIRE_INSTALL=1
    _test_cycle 1
}

cmd_test_all() {
    require_linux; require_kvm; require_tools
    local -a passed=() failed=()
    local port="$SSH_PORT"
    local script
    script="$_SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
    # Every distro × both variants. Each cell gets its own forwarded port so a
    # leftover from a prior cell can't collide, and its own subshell so one
    # cell's failure (or teardown trap) doesn't abort the matrix.
    for sub in test test-ready; do
        for d in "${ALL_DISTROS[@]}"; do
            echo
            echo "########################################################"
            echo "# $d  ($sub)"
            echo "########################################################"
            if ( DISTRO="$d" SSH_PORT="$port" \
                    BB_TEST_DISTRO="$d" BB_TEST_SSH_PORT="$port" \
                    bash "$script" "$sub" ); then
                passed+=("$d/$sub")
            else
                failed+=("$d/$sub")
            fi
            port=$(( port + 1 ))
        done
    done
    echo
    echo "== matrix summary =="
    echo "  PASS: ${passed[*]:-(none)}"
    echo "  FAIL: ${failed[*]:-(none)}"
    [ "${#failed[@]}" -eq 0 ]
}

case "${1:-}" in
    test)       cmd_test ;;
    test-ready) cmd_test_ready ;;
    test-all)   cmd_test_all ;;
    up)         cmd_up ;;
    prereqs)    cmd_prereqs ;;
    run)        cmd_run ;;
    status)     cmd_status ;;
    down)       cmd_down ;;
    ssh)        cmd_ssh ;;
    *) echo "usage: $0 {test|test-ready|test-all|up|prereqs|run|status|down|ssh}  (distro via BB_TEST_DISTRO)" >&2; exit 2 ;;
esac
