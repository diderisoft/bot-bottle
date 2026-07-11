#!/usr/bin/env bash
# One-time privileged network setup for the Firecracker backend.
#
# Creates a pool of point-to-point TAP devices (owned by the invoking
# user so the backend can open them without root at launch) and a
# dedicated nftables table that isolates every VM: a bottle VM can
# reach only its own sidecar (published on the host-side TAP IP) and
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
# Env overrides (must match the backend's util.py constants):
#   BOT_BOTTLE_FC_POOL_SIZE   number of slots (default 8)
#   BOT_BOTTLE_FC_IP_BASE     base IPv4 of the /31 pool (default 10.243.0.0)
#   BOT_BOTTLE_FC_IFACE_PREFIX  TAP name prefix (default bbfc)
#   BOT_BOTTLE_FC_OWNER       owning user (default $SUDO_USER or $USER)

set -euo pipefail

POOL_SIZE="${BOT_BOTTLE_FC_POOL_SIZE:-8}"
IP_BASE="${BOT_BOTTLE_FC_IP_BASE:-10.243.0.0}"
PREFIX="${BOT_BOTTLE_FC_IFACE_PREFIX:-bbfc}"
OWNER="${BOT_BOTTLE_FC_OWNER:-${SUDO_USER:-$USER}}"
TABLE="bot_bottle_fc"

# Sidecar ports (must match the backend). egress=9099, supervise=9100,
# git-http=9420. Reached by the VM at its host-side TAP IP.
SIDECAR_PORTS="9099,9100,9420"

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

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "error: '$1' needs root (run under sudo)" >&2
        exit 1
    fi
}

cmd_up() {
    require_root up
    echo "firecracker net pool: $POOL_SIZE slots, base $IP_BASE, owner $OWNER"

    # VM->sidecar traffic is DNAT'd to the sidecar container and
    # forwarded, so forwarding must be enabled (Docker also sets this).
    sysctl -qw net.ipv4.ip_forward=1

    for i in $(seq 0 $((POOL_SIZE-1))); do
        local dev host
        dev="$(iface "$i")" ; host="$(host_ip "$i")"
        if ip link show "$dev" >/dev/null 2>&1; then
            ip link set "$dev" down 2>/dev/null || true
            ip tuntap del dev "$dev" mode tap 2>/dev/null || true
        fi
        ip tuntap add dev "$dev" mode tap user "$OWNER"
        ip addr add "$host/31" dev "$dev"
        ip link set "$dev" up
        echo "  $dev  host=$host  guest=$(guest_ip "$i")  owner=$OWNER"
    done

    _install_nft
    echo "nftables table inet $TABLE installed (fail-closed boundary)"
    echo "done."
}

_install_nft() {
    # Own table: dropping only matches our bbfc* interfaces, so no other
    # tool's traffic is affected. Priority -10 runs before Docker's
    # filter hooks (priority 0); a drop here is terminal for the packet.
    #
    #   forward: VM egress is DNAT'd to the sidecar (established via
    #     `ct status dnat`); return traffic via `ct state established`.
    #     Anything else from a VM is dropped -> no route to the internet
    #     or the rest of the host except through the sidecar proxy.
    #   input: a VM never needs host-local delivery (its sidecar is
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

cmd_down() {
    require_root down
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
    echo "taps:"
    for i in $(seq 0 $((POOL_SIZE-1))); do
        local dev ; dev="$(iface "$i")"
        if ip -brief addr show "$dev" >/dev/null 2>&1; then
            ip -brief addr show "$dev" | sed 's/^/  /'
        fi
    done
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    *) echo "usage: $0 {up|down|status}" >&2 ; exit 2 ;;
esac
