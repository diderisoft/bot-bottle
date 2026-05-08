#!/usr/bin/env bash
# Docker network plumbing for the per-agent egress-proxy topology
# (PRD 0001).
#
# The egress design (see docs/research/pipelock-assessment.md
# §"Deployment topology") puts the agent container on a Docker
# `--internal` network — Docker omits the default gateway from
# `internal: true` networks at the iptables level inside the engine /
# LinuxKit VM, so the only address the agent can reach is the pipelock
# sidecar attached to the same network. The pipelock sidecar itself
# also needs egress to the upstream internet, so it is placed on a
# second (user-defined bridge) network as well. We deliberately do
# NOT use Docker's legacy `bridge` network for this: the legacy bridge
# has no embedded DNS resolver, so pipelock would be unable to resolve
# `api.anthropic.com` and Claude Code traffic would dead-end. Only
# user-defined bridges run Docker's built-in DNS, so we create one
# per agent.
#
# This module is the network-only half of that split: create / attach
# / teardown of both the per-agent internal network and the per-agent
# user-defined egress bridge, with no pipelock specifics. Keeping
# pipelock-agnostic helpers here means a future PRD can reuse them
# for a different sidecar (e.g. an iptables-only layer) without
# entangling the two concerns.
#
# Naming: claude-bottle-net-<slug> (internal),
# claude-bottle-egress-<slug> (egress). On conflict we append a
# numeric suffix (-2, -3, ...) to mirror the container-naming scheme
# in cli.sh, so two parallel starts of the same agent get distinct
# networks.
#
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_NETWORK_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_NETWORK_SOURCED=1

_iso_lib_network_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_network_dir}/log.sh"

# network_name_for_slug <slug> — prints the canonical internal-network
# name for a given agent slug. No conflict resolution; that lives in
# network_create_internal.
network_name_for_slug() {
  local slug="${1:?network_name_for_slug: missing slug}"
  printf 'claude-bottle-net-%s' "$slug"
}

# network_egress_name_for_slug <slug> — prints the canonical egress-network
# name for a given agent slug. No conflict resolution; that lives in
# network_create_egress.
network_egress_name_for_slug() {
  local slug="${1:?network_egress_name_for_slug: missing slug}"
  printf 'claude-bottle-egress-%s' "$slug"
}

# network_exists <name> — returns 0 if the named docker network exists,
# else 1. Uses `docker network inspect` (not `docker network ls -f name=...`)
# because the latter does substring matching, which would falsely report
# claude-bottle-net-foo as existing when only claude-bottle-net-foo-2 was
# present.
network_exists() {
  local name="${1:?network_exists: missing network name}"
  docker network inspect "$name" >/dev/null 2>&1
}

# _network_create_with_prefix <prefix> <internal: 0|1>
#
# Internal helper. Creates a per-agent Docker network whose name is
# <prefix> (with -2, -3, ... appended on conflict, capped at 100).
# When <internal> is 1, the network is created with `--internal` (no
# default gateway). When 0, it's a plain user-defined bridge with
# upstream connectivity. Echoes the resolved name on stdout.
_network_create_with_prefix() {
  local base="${1:?_network_create_with_prefix: missing prefix}"
  local internal_flag="${2:?_network_create_with_prefix: missing internal flag}"

  local name="$base"
  local _suffix=2
  while network_exists "$name"; do
    name="${base}-${_suffix}"
    _suffix=$((_suffix + 1))
    if [ "$_suffix" -gt 100 ]; then
      die "could not find a free network name after ${base}-99; clean up old networks with 'docker network rm <name>'"
    fi
  done

  local kind="bridge (egress)"
  local args=()
  if [ "$internal_flag" = "1" ]; then
    kind="internal"
    args+=(--internal)
  fi
  info "creating ${kind} network ${name}"
  # Defaults give us a bridge driver with Docker-managed addressing,
  # which is what we want for both internal and egress networks.
  if ! docker network create "${args[@]}" "$name" >/dev/null; then
    die "docker network create ${args[*]} ${name} failed"
  fi
  printf '%s' "$name"
}

# network_create_internal <slug>
#
# Creates a Docker `--internal` network for the agent and prints the
# resolved network name on stdout. If the canonical name is already
# taken, appends -2, -3, ... (capped at 100, matching the
# container-name retry loop in cli.sh) until a free name is found.
#
# `--internal` is the load-bearing flag: Docker creates the bridge
# without a default route, so the agent container attached here cannot
# reach the public internet directly. The pipelock sidecar (attached
# to both this network and a per-agent egress network) is the only
# egress route.
#
# Side effect: emits one info line naming the network actually created.
network_create_internal() {
  local slug="${1:?network_create_internal: missing slug}"
  local base
  base="$(network_name_for_slug "$slug")"
  _network_create_with_prefix "$base" 1
}

# network_create_egress <slug>
#
# Creates a per-agent user-defined bridge network used by the pipelock
# sidecar for upstream egress, and prints the resolved network name on
# stdout. Conflict resolution mirrors network_create_internal.
#
# We use a user-defined bridge (NOT the legacy `bridge` network)
# because only user-defined bridges run Docker's embedded DNS resolver
# — pipelock needs DNS to resolve `api.anthropic.com` and similar
# upstream hostnames. The legacy `bridge` network would force pipelock
# onto the host's resolv.conf and fail in environments where Docker
# Desktop's NAT path is the only working DNS route.
#
# Side effect: emits one info line naming the network actually created.
network_create_egress() {
  local slug="${1:?network_create_egress: missing slug}"
  local base
  base="$(network_egress_name_for_slug "$slug")"
  _network_create_with_prefix "$base" 0
}

# network_attach <network> <container>
#
# Attaches an already-running container to the named network. Used to
# add the pipelock sidecar to a second (default-bridge) network so it
# has upstream egress, while staying reachable from the agent on the
# internal network.
#
# Note: for the agent container itself we pass `--network <name>` to
# `docker run` directly in cli.sh rather than using this function. The
# agent never touches anything except the internal network.
network_attach() {
  local network="${1:?network_attach: missing network name}"
  local container="${2:?network_attach: missing container name}"
  if ! docker network connect "$network" "$container" >/dev/null 2>&1; then
    die "docker network connect ${network} ${container} failed"
  fi
}

# network_remove <name>
#
# Removes the named network. Idempotent: a missing network is treated
# as success so this can be called unconditionally from a teardown
# trap. A network that still has containers attached will fail to
# remove; the caller is expected to tear those containers down first.
network_remove() {
  local name="${1:?network_remove: missing network name}"
  if ! network_exists "$name"; then
    return 0
  fi
  if ! docker network rm "$name" >/dev/null 2>&1; then
    # Don't `die` here: this runs in cleanup paths where we'd rather
    # warn and continue than abort and leave more orphans behind.
    warn "failed to remove network ${name}; clean up with 'docker network rm ${name}'"
    return 1
  fi
}
