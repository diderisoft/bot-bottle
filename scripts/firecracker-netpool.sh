#!/usr/bin/env bash
# One-time privileged network setup for the Firecracker backend.
#
# Creates a pool of point-to-point TAP devices (owned by the invoking
# user so the backend can open them without root at launch) and a
# dedicated nftables table that isolates every VM: a bottle VM can
# reach only its own gateway (published on the host-side TAP IP) and
# nothing else on the host or network.
#
# Why a pool + one-time setup: creating a TAP and assigning it an IP
# needs CAP_NET_ADMIN. Pre-creating user-owned, pre-addressed TAPs
# means `./cli.py start` never needs root. The nft table is static
# (keyed on the `bbfc*` interface wildcard), so it covers every slot
# without per-launch changes.
#
# Design notes:
#   * No shared bridge — each slot is an isolated /31 host<->guest link,
#     so there are no bridge name/subnet collisions with docker0,
#     virbr0 (libvirt), cni0 (k8s) or br-* (docker networks).
#   * Own nftables table `bot_bottle_fc` — independent of the iptables
#     filter/nat tables Docker/ufw/firewalld use, so nothing is stomped.
#   * Default IP block is an obscure RFC-1918 /16 (10.243.0.0/16),
#     chosen to dodge the usual occupants (docker 172.17-31, libvirt
#     192.168.122, k8s 10.42/10.244, home LANs). NOT 100.64.0.0/10 —
#     that's RFC-6598 CGNAT, which Tailscale hands node addresses from.
#
# NixOS: imperative rules here do NOT survive nixos-rebuild. Use the
# declarative module in nix/firecracker-netpool.nix instead (exposed as
# the flake output nixosModules.firecracker-netpool).
#
# Usage:
#   sudo ./scripts/firecracker-netpool.sh up
#   sudo ./scripts/firecracker-netpool.sh down
#   ./scripts/firecracker-netpool.sh status
#
# Pool params default to the shared single-source file
# (bot_bottle/backend/firecracker/netpool.defaults.env); a matching
# env var overrides its key:
#   BOT_BOTTLE_FC_POOL_SIZE     number of slots
#   BOT_BOTTLE_FC_IP_BASE       base IPv4 of the /31 pool
#   BOT_BOTTLE_FC_IFACE_PREFIX  TAP name prefix
#   BOT_BOTTLE_FC_NFT_TABLE     isolation table name
#   BOT_BOTTLE_FC_OWNER         owning user (default $SUDO_USER or $USER)
#   BOT_BOTTLE_FC_GROUP         owning group; if set, TAPs are group-owned
#                               instead of user-owned, so any group member
#                               (e.g. an interactive user + a CI runner
#                               user) can open the pool. Overrides OWNER.

set -euo pipefail

# The single source of the pool defaults, shared with netpool.py and the
# Nix module. The Nix path passes every value as env (the script runs
# from the store, detached from this file), so this lookup only matters
# on the direct/sudo path where the script sits in the repo tree.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_DEFAULTS="$_SCRIPT_DIR/../bot_bottle/backend/firecracker/netpool.defaults.env"
_default() {  # value of KEY=... from the shared file; empty if unavailable
    [ -f "$_DEFAULTS" ] || return 0
    sed -n "s/^$1=//p" "$_DEFAULTS" | tail -1
}

POOL_SIZE="${BOT_BOTTLE_FC_POOL_SIZE:-$(_default BOT_BOTTLE_FC_POOL_SIZE)}"
IP_BASE="${BOT_BOTTLE_FC_IP_BASE:-$(_default BOT_BOTTLE_FC_IP_BASE)}"
PREFIX="${BOT_BOTTLE_FC_IFACE_PREFIX:-$(_default BOT_BOTTLE_FC_IFACE_PREFIX)}"
TABLE="${BOT_BOTTLE_FC_NFT_TABLE:-$(_default BOT_BOTTLE_FC_NFT_TABLE)}"
ORCH_IFACE="${BOT_BOTTLE_FC_ORCH_IFACE:-$(_default BOT_BOTTLE_FC_ORCH_IFACE)}"
OWNER="${BOT_BOTTLE_FC_OWNER:-${SUDO_USER:-$USER}}"
GROUP="${BOT_BOTTLE_FC_GROUP:-}"

# Fail loudly rather than provisioning a half/empty range if a value
# resolved to nothing (env unset AND the shared file unreadable).
for _v in POOL_SIZE IP_BASE PREFIX TABLE ORCH_IFACE; do
    [ -n "${!_v}" ] || { echo "error: $_v unresolved (set BOT_BOTTLE_FC_* or fix $_DEFAULTS)" >&2; exit 1; }
done

# Gateway ports (must match the backend). egress=9099, supervise=9100,
# git-http=9420. Reached by the VM at its host-side TAP IP.
GATEWAY_PORTS="9099,9100,9420"

# --- IP math ---------------------------------------------------------
# Slot i occupies the /31 {base+2i, base+2i+1}: host = base+2i (the
# gateway the VM routes through), guest = base+2i+1 (the VM's address).
_ip_to_int() {
    local IFS=. ; read -r a b c d <<<"$1" ; echo $(( (a<<24) + (b<<16) + (c<<8) + d ))
}
_int_to_ip() {
    local n=$1 ; echo "$(( (n>>24)&255 )).$(( (n>>16)&255 )).$(( (n>>8)&255 )).$(( n&255 ))"
}
host_ip() { _int_to_ip $(( $(_ip_to_int "$IP_BASE") + 2*$1 )); }
guest_ip() { _int_to_ip $(( $(_ip_to_int "$IP_BASE") + 2*$1 + 1 )); }
iface()   { echo "${PREFIX}$1"; }

# Orchestrator/gateway VM link: a /31 at the TOP of the IP_BASE /16
# (host x.y.255.0, guest x.y.255.1), well clear of the agent pool near
# the bottom of the block. Must match netpool.py:orch_slot().
_orch_base() { echo $(( ($(_ip_to_int "$IP_BASE") & 0xFFFF0000) + 0xFF00 )); }
orch_host()  { _int_to_ip "$(_orch_base)"; }
orch_guest() { _int_to_ip $(( $(_orch_base) + 1 )); }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "error: '$1' needs root (run under sudo)" >&2
        exit 1
    fi
}

cmd_up() {
    require_root up
    # Group ownership (any member can open the pool) overrides single-user
    # ownership. The kernel lets a TAP's owning-group members attach.
    if [ -n "$GROUP" ]; then
        own_args=(group "$GROUP") ; own_desc="group=$GROUP"
    else
        own_args=(user "$OWNER") ; own_desc="owner=$OWNER"
    fi
    echo "firecracker net pool: $POOL_SIZE slots, base $IP_BASE, $own_desc"

    # VM->gateway traffic is DNAT'd to the gateway container and
    # forwarded, so forwarding must be enabled (Docker also sets this).
    sysctl -qw net.ipv4.ip_forward=1

    for i in $(seq 0 $((POOL_SIZE-1))); do
        local dev host
        dev="$(iface "$i")" ; host="$(host_ip "$i")"
        # Non-destructive + idempotent: only create a missing TAP (tearing
        # an existing one down would cut a running VM), and `addr replace`
        # is safe to re-run. Ownership is fixed at creation, so to change
        # owner/group run `down` then `up`.
        ip link show "$dev" >/dev/null 2>&1 \
            || ip tuntap add dev "$dev" mode tap "${own_args[@]}"
        ip addr replace "$host/31" dev "$dev"
        ip link set "$dev" up
        echo "  $dev  host=$host  guest=$(guest_ip "$i")  $own_desc"
    done

    # The orchestrator/gateway VM's dedicated link. Same rootless-open
    # ownership as the pool, but NAT'd to the internet (below) — it is
    # trusted infra, not an isolated agent slot.
    ip link show "$ORCH_IFACE" >/dev/null 2>&1 \
        || ip tuntap add dev "$ORCH_IFACE" mode tap "${own_args[@]}"
    ip addr replace "$(orch_host)/31" dev "$ORCH_IFACE"
    ip link set "$ORCH_IFACE" up
    echo "  $ORCH_IFACE  host=$(orch_host)  guest=$(orch_guest)  (NAT'd egress)  $own_desc"

    _install_nft
    echo "nftables table inet $TABLE installed (fail-closed boundary)"
    _install_orch_egress
    echo "orchestrator egress installed ($ORCH_IFACE -> NAT out)"
    echo "done."
}

_install_nft() {
    # Own table: dropping only matches our bbfc* interfaces, so no other
    # tool's traffic is affected. Priority -10 runs before Docker's
    # filter hooks (priority 0); a drop here is terminal for the packet.
    #
    #   forward: VM egress is DNAT'd to the gateway (established via
    #     `ct status dnat`); return traffic via `ct state established`.
    #     Anything else from a VM is dropped -> no route to the internet
    #     or the rest of the host except through the gateway proxy.
    #   input: a VM never needs host-local delivery (its gateway is
    #     reached via DNAT->forward), so drop all direct input from VMs
    #     -> host services bound on 0.0.0.0 are unreachable from the VM.
    nft -f - <<EOF
table inet $TABLE {
    chain forward {
        type filter hook forward priority -10; policy accept;
        iifname != "${PREFIX}*" return
        ct state established,related accept
        ct status dnat accept
        drop
    }
    chain input {
        type filter hook input priority -10; policy accept;
        iifname != "${PREFIX}*" return
        ct state established,related accept
        drop
    }
}
EOF
}

# Give the orchestrator/gateway VM real internet egress (agent VMs get
# none — that's the isolation table above). Three parts, because the
# path must work both during bootstrap (Docker still present) and after
# Docker is removed:
#   * masquerade   — SNAT the orch guest /31 out the host uplink so its
#                    RFC-1918 address can reach the internet.
#   * nft forward  — accept the orch link's forward path (load-bearing
#                    on a pure-nft host whose FORWARD policy drops; a
#                    harmless no-op where forwarding is already open).
#                    It never drops, so it can't weaken the isolation
#                    table's agent drops.
#   * DOCKER-USER  — during bootstrap Docker's FORWARD chain policy is
#                    DROP; its sanctioned DOCKER-USER hook is the only
#                    place a user ACCEPT survives. Best-effort + guarded
#                    (skipped once Docker is gone).
_install_orch_egress() {
    nft -f - <<EOF
table inet ${TABLE}_nat {
    chain forward {
        type filter hook forward priority -10; policy accept;
        iifname "$ORCH_IFACE" accept
        oifname "$ORCH_IFACE" ct state established,related accept
    }
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
        ip saddr $(orch_guest) oifname != "$ORCH_IFACE" masquerade
    }
}
EOF
    _docker_user_orch add
}

# Insert (add) or delete (del) the DOCKER-USER ACCEPT rules for the
# orchestrator link, idempotently, only when the chain exists.
_docker_user_orch() {
    local op="$1" flag
    command -v iptables >/dev/null 2>&1 || return 0
    iptables -t filter -L DOCKER-USER >/dev/null 2>&1 || return 0
    for flag in "-i" "-o"; do
        if [ "$op" = add ]; then
            iptables -C DOCKER-USER "$flag" "$ORCH_IFACE" -j ACCEPT 2>/dev/null \
                || iptables -I DOCKER-USER "$flag" "$ORCH_IFACE" -j ACCEPT
        else
            iptables -D DOCKER-USER "$flag" "$ORCH_IFACE" -j ACCEPT 2>/dev/null || true
        fi
    done
}

cmd_down() {
    require_root down
    _docker_user_orch del
    nft delete table inet "${TABLE}_nat" 2>/dev/null || true
    if ip link show "$ORCH_IFACE" >/dev/null 2>&1; then
        ip link set "$ORCH_IFACE" down 2>/dev/null || true
        ip tuntap del dev "$ORCH_IFACE" mode tap 2>/dev/null || true
        echo "  removed $ORCH_IFACE"
    fi
    nft delete table inet "$TABLE" 2>/dev/null || true
    for i in $(seq 0 $((POOL_SIZE-1))); do
        local dev ; dev="$(iface "$i")"
        if ip link show "$dev" >/dev/null 2>&1; then
            ip link set "$dev" down 2>/dev/null || true
            ip tuntap del dev "$dev" mode tap 2>/dev/null || true
            echo "  removed $dev"
        fi
    done
    echo "done."
}

cmd_status() {
    echo "table inet $TABLE:"
    nft list table inet "$TABLE" 2>/dev/null || echo "  (absent)"
    echo "table inet ${TABLE}_nat (orchestrator egress):"
    nft list table inet "${TABLE}_nat" 2>/dev/null || echo "  (absent)"
    echo "taps:"
    for i in $(seq 0 $((POOL_SIZE-1))); do
        local dev ; dev="$(iface "$i")"
        if ip -brief addr show "$dev" >/dev/null 2>&1; then
            ip -brief addr show "$dev" | sed 's/^/  /'
        fi
    done
    if ip -brief addr show "$ORCH_IFACE" >/dev/null 2>&1; then
        ip -brief addr show "$ORCH_IFACE" | sed 's/^/  /'
    fi
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    *) echo "usage: $0 {up|down|status}" >&2 ; exit 2 ;;
esac
