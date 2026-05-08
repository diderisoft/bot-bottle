#!/usr/bin/env bash
# SSH helpers. Validates ssh entries from claude-bottle.json, then sets up SSH
# inside the container via a root-owned ssh-agent so the `node` user (Claude)
# can use the keys for SSH operations but cannot read the key bytes.
#
# Why an in-container agent (not bind-mounted from host): Docker Desktop on
# macOS does not forward Unix-domain socket connect() across the macOS↔Linux
# VM boundary — connect() returns ENOTSUP. Running ssh-agent inside the
# container sidesteps that entirely and keeps the same isolation guarantee.
#
# How the isolation works:
#   - Keys are docker cp'd to /root/.claude-bottle-keys/ (mode 700, root-owned).
#     /root itself is mode 700 in the node:22-slim base image, so node (uid
#     1000) cannot even traverse into it.
#   - ssh-agent runs as root, listening on /run/claude-bottle-agent.sock. Each
#     key is loaded with ssh-add, then the key file is deleted. The bytes
#     now live only in the agent process's memory.
#   - The agent socket stays root-only. OpenSSH's ssh-agent enforces a
#     SO_PEERCRED-based UID match: it rejects every connection whose peer
#     euid is neither 0 nor the agent's own uid. chmod'ing the socket open
#     does *not* defeat this — the kernel-level check still rejects node.
#   - To bridge that, a root-owned socat forwarder listens on
#     /run/claude-bottle-agent-public.sock (mode 666) and proxies bytes to the
#     real agent socket. From the agent's view, socat (uid 0) is the peer
#     and passes the UID check. From node's view, the public socket is the
#     accessible endpoint.
#   - node cannot ptrace the root-owned agent or socat (no CAP_SYS_PTRACE in
#     a default container), so /proc/<pid>/mem is off-limits and the key
#     bytes never leave root-owned memory.
#   - ~/.ssh/config in node's home points each Host at the public socket via
#     IdentityAgent, so SSH always reaches the forwarder regardless of
#     SSH_AUTH_SOCK.
#
# Limitation: keys must be passphrase-less. ssh-add prompts on /dev/tty for
# passphrases, but our docker exec has no TTY. Adding SSH_ASKPASS support is
# possible but not implemented in v1.
#
# Each ssh entry is a JSON object (jq -c) with keys:
#   Host           SSH Host alias
#   IdentityFile   absolute path to the private key file on the host
#   Hostname       the actual hostname or IP
#   User           SSH username
#   Port           SSH port (number)
#   KnownHostKey   (optional) host public key — written to known_hosts under
#                  both the Host alias and the Hostname so the lookup works
#                  whether SSH connects via the alias or the raw IP/host.
#
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_SSH_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_SSH_SOURCED=1

_iso_lib_ssh_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_ssh_dir}/log.sh"

# ssh_validate_entries <json_object>... — checks that each entry has the
# required fields and that its IdentityFile exists on the host. Dies on the
# first problem.
ssh_validate_entries() {
  local entry name key
  for entry in "$@"; do
    name="$(printf '%s' "$entry" | jq -r '.Host // empty')"
    key="$(printf '%s' "$entry" | jq -r '.IdentityFile // empty')"
    [ -n "$name" ] || die "ssh entry missing required field 'Host': ${entry}"
    [ -n "$key" ]  || die "ssh entry '${name}' missing required field 'IdentityFile'"
    # Expand a leading ~ so callers can use ~/... paths.
    key="${key/#\~/$HOME}"
    [ -f "$key" ] || die "ssh key file not found for host '${name}': ${key}"
  done
}

# ssh_setup <container> <stage_dir> <json_object>... — sets up SSH in the
# container so node (Claude) can authenticate using each entry's key without
# the key file being readable by node.
#
# Lifecycle:
#   1. Create ~/.ssh (700) for node and /root/.claude-bottle-keys (700) for root.
#   2. docker cp each key into /root/.claude-bottle-keys/, chown root, chmod 600.
#   3. Boot ssh-agent at /run/claude-bottle-agent.sock (root-only), ssh-add each
#      key, delete the key file, rmdir the keys staging dir.
#   4. Boot a root-owned socat forwarder on /run/claude-bottle-agent-public.sock
#      (mode 666) proxying to the agent socket. Bridges the UID-match check
#      that would otherwise reject node's connections (see file header).
#   5. Install ~/.ssh/config (IdentityAgent → public socket) and
#      ~/.ssh/known_hosts under node's home.
ssh_setup() {
  local container="${1:?ssh_setup: missing container}"
  local stage_dir="${2:?ssh_setup: missing stage dir}"
  # proxy_host_port is the pipelock sidecar as <host>:<port> (no scheme).
  # Used as socat's PROXY: argument so the agent can reach SSH hosts
  # over the agent's --internal network — the only egress route is the
  # pipelock CONNECT proxy. Required.
  local proxy_host_port="${3:?ssh_setup: missing proxy_host_port}"
  shift 3

  local container_home="${CLAUDE_BOTTLE_CONTAINER_HOME:-/home/node}"
  local container_ssh="${container_home}/.ssh"
  local agent_socket="/run/claude-bottle-agent.sock"
  local public_socket="/run/claude-bottle-agent-public.sock"
  local keys_dir="/root/.claude-bottle-keys"

  # ~/.ssh for node (700, owned by node).
  docker exec -u 0 "$container" mkdir -p "$container_ssh" >/dev/null
  docker exec -u 0 "$container" chown node:node "$container_ssh" >/dev/null
  docker exec -u 0 "$container" chmod 700 "$container_ssh" >/dev/null

  # /root/.claude-bottle-keys for root (700, root-owned). /root is already 700
  # in node:22-slim, so node can't traverse here either way; setting both
  # layers keeps the intent explicit.
  docker exec -u 0 "$container" mkdir -p "$keys_dir" >/dev/null
  docker exec -u 0 "$container" chown root:root "$keys_dir" >/dev/null
  docker exec -u 0 "$container" chmod 700 "$keys_dir" >/dev/null

  local config_file="${stage_dir}/ssh_config"
  local known_hosts_file="${stage_dir}/ssh_known_hosts"
  : > "$config_file"
  chmod 600 "$config_file"
  : > "$known_hosts_file"
  chmod 600 "$known_hosts_file"

  local entry name key hostname user port known_host_key key_basename container_key_path
  local container_key_paths=()
  for entry in "$@"; do
    name="$(printf '%s' "$entry" | jq -r '.Host')"
    key="$(printf '%s' "$entry" | jq -r '.IdentityFile')"
    hostname="$(printf '%s' "$entry" | jq -r '.Hostname')"
    user="$(printf '%s' "$entry" | jq -r '.User')"
    port="$(printf '%s' "$entry" | jq -r '.Port')"
    known_host_key="$(printf '%s' "$entry" | jq -r '.KnownHostKey // empty')"

    key="${key/#\~/$HOME}"
    key_basename="$(basename "$key")"
    container_key_path="${keys_dir}/${key_basename}"

    info "copying ssh key for '${name}' -> ${container} (root-only staging)"
    docker cp "$key" "${container}:${container_key_path}" >/dev/null
    docker exec -u 0 "$container" chown root:root "$container_key_path" >/dev/null
    docker exec -u 0 "$container" chmod 600 "$container_key_path" >/dev/null

    container_key_paths+=("$container_key_path")

    # No IdentityFile — IdentityAgent points SSH at the public (forwarded)
    # socket. Pointing at the real agent socket directly would be rejected
    # by ssh-agent's UID-match check (see file header).
    #
    # ProxyCommand tunnels the SSH connection through pipelock via HTTP
    # CONNECT. The agent container has no default route (--internal
    # network); pipelock is the only path to anywhere. socat's PROXY:
    # mode does CONNECT host:port to the proxy. %h / %p expand to this
    # block's HostName / Port. The SSH host must also appear in
    # pipelock's allowlist — pipelock_effective_allowlist auto-includes
    # bottle.ssh[].Hostname entries so this just works for declared
    # hosts.
    printf 'Host %s\n  HostName %s\n  User %s\n  Port %s\n  IdentityAgent %s\n  ProxyCommand socat - PROXY:%s:%%h:%%p,proxyport=%s\n\n' \
      "$name" "$hostname" "$user" "$port" "$public_socket" \
      "${proxy_host_port%:*}" "${proxy_host_port##*:}" >> "$config_file"

    if [ -n "$known_host_key" ]; then
      # Write under both the Host alias and the Hostname so SSH finds the key
      # whether the connection uses the alias (`ssh <name>`) or a raw IP/host
      # (e.g. git remote URLs that bypass the alias). Skip the duplicate when
      # they're already the same string.
      if [ "$port" = "22" ]; then
        printf '%s %s\n' "$name" "$known_host_key" >> "$known_hosts_file"
        [ "$hostname" != "$name" ] && printf '%s %s\n' "$hostname" "$known_host_key" >> "$known_hosts_file"
      else
        printf '[%s]:%s %s\n' "$name" "$port" "$known_host_key" >> "$known_hosts_file"
        [ "$hostname" != "$name" ] && printf '[%s]:%s %s\n' "$hostname" "$port" "$known_host_key" >> "$known_hosts_file"
      fi
    fi
  done

  # Boot the agent, load each key, delete the key files, then start the
  # root-owned socat forwarder that exposes a node-accessible socket. One
  # docker exec so the whole sequence is atomic — if any step fails (e.g.
  # passphrase-protected key), set -e dies before we leave behind a
  # half-initialized agent.
  info "starting in-container ssh-agent at ${agent_socket} (forwarded via ${public_socket})"
  local setup_script="set -eu
ssh-agent -a ${agent_socket} >/dev/null
"
  local kp
  for kp in "${container_key_paths[@]}"; do
    setup_script+="SSH_AUTH_SOCK=${agent_socket} ssh-add ${kp}
rm -f ${kp}
"
  done
  setup_script+="rmdir ${keys_dir} 2>/dev/null || true
# Start the forwarder. Detach from the calling shell so it survives this
# docker exec returning. socat (running as root) connects to the agent on
# node's behalf; the agent's UID-match check sees uid 0 and accepts.
nohup socat UNIX-LISTEN:${public_socket},fork,reuseaddr,mode=666 UNIX-CONNECT:${agent_socket} </dev/null >/dev/null 2>&1 &
# Wait briefly for the forwarder to bind. Without this, an SSH client that
# fires immediately after this script returns can race the listener and hit
# ENOENT/ECONNREFUSED on the public socket.
i=0
while [ \$i -lt 20 ]; do
  [ -S ${public_socket} ] && break
  i=\$((i + 1))
  sleep 0.1
done
[ -S ${public_socket} ] || { echo 'claude-bottle: socat forwarder failed to bind ${public_socket}' >&2; exit 1; }
"
  docker exec -u 0 "$container" sh -c "$setup_script"

  info "writing ${container_ssh}/config"
  docker cp "$config_file" "${container}:${container_ssh}/config" >/dev/null
  docker exec -u 0 "$container" chown node:node "${container_ssh}/config" >/dev/null
  docker exec -u 0 "$container" chmod 600 "${container_ssh}/config" >/dev/null

  if [ -s "$known_hosts_file" ]; then
    info "writing ${container_ssh}/known_hosts"
    docker cp "$known_hosts_file" "${container}:${container_ssh}/known_hosts" >/dev/null
    docker exec -u 0 "$container" chown node:node "${container_ssh}/known_hosts" >/dev/null
    docker exec -u 0 "$container" chmod 600 "${container_ssh}/known_hosts" >/dev/null
  fi
}
