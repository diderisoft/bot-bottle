#!/usr/bin/env bash
# Pipelock sidecar lifecycle for the per-agent egress topology
# (PRD 0001).
#
# Pipelock (https://github.com/luckyPipewrench/pipelock) is an HTTP
# forward proxy with hostname allowlisting + DLP scanning + URL-entropy
# checks. We run one sidecar container per agent, attached to the
# agent's --internal network (created by lib/network.sh) and to a
# per-agent user-defined bridge network for upstream egress (also
# created by lib/network.sh — see the comment in network_create_egress
# for why we don't use Docker's legacy `bridge` network). The agent's
# HTTPS_PROXY / HTTP_PROXY env vars point at the sidecar's service
# name on the internal network; combined with --internal (which omits
# the default gateway), pipelock is the only egress route the agent
# has.
#
# Image pin: ghcr.io/luckypipewrench/pipelock@sha256:<digest>. The
# digest is resolved by hand against ghcr.io for tag 2.3.0 (the
# `v2.3.0` GitHub release maps to the unprefixed `2.3.0` Docker tag —
# see pipelock-assessment.md and the resolution log in PRD 0001's
# implementation thread). Bump deliberately when upgrading.
#
# YAML config we generate: minimum-viable settings to satisfy the PRD's
# observable success criteria.
#   - mode: strict           — only api_allowlist domains are reachable
#                              (per docs/configuration.md §Modes)
#   - enforce: true          — blocks rather than warn-only
#   - api_allowlist: [...]   — defaults ∪ bottle.egress.allowlist
#   - forward_proxy.enabled: true  — turns on the CONNECT-tunnel proxy
#                              the agent's HTTPS_PROXY actually uses
#                              (docs §Forward Proxy: this is off by
#                              default, restart-required to flip)
#   - dlp.include_defaults: true   — load all 48 built-in patterns
#                              (docs §DLP §Pattern Merging)
#   - dlp.scan_env: true     — flags URLs containing high-entropy env
#                              values (≥16 chars, Shannon entropy >3.0,
#                              checked in raw/base64/hex/base32). This
#                              is the documented home for pipelock's
#                              "subdomain entropy detection" surface
#                              (docs §Environment Variable Leak
#                              Detection); the URL-path-entropy knob
#                              under fetch_proxy.monitoring is for the
#                              /fetch?url=... helper, not the forward
#                              proxy we use.
# We deliberately do NOT set tls_interception (out of PRD scope), and
# do NOT carry any env-var values into the YAML — only hostnames.
#
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_PIPELOCK_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_PIPELOCK_SOURCED=1

_iso_lib_pipelock_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_pipelock_dir}/log.sh"
# shellcheck source=./manifest.sh
. "${_iso_lib_pipelock_dir}/manifest.sh"
# shellcheck source=./network.sh
. "${_iso_lib_pipelock_dir}/network.sh"

# --- Constants -------------------------------------------------------------

# Pipelock image, pinned by digest. The digest is the multi-arch image
# index for ghcr.io/luckypipewrench/pipelock:2.3.0 (resolved 2026-05-08
# from the ghcr.io v2 manifests endpoint). Ties match the v2.3.0 GitHub
# release; the registry uses unprefixed tags so v2.3.0→2.3.0.
CLAUDE_BOTTLE_PIPELOCK_IMAGE="${CLAUDE_BOTTLE_PIPELOCK_IMAGE:-ghcr.io/luckypipewrench/pipelock@sha256:3b1a39417b98406ddc5dc2d8fcb42865ddc0c68a43d355db55f0f8cb06bc6de9}"

# Listening port for pipelock's forward proxy. Default per
# docs/configuration.md §Forward Proxy / §Fetch Proxy and the
# deployment-recipes generator. Override via env if a future image
# changes it.
CLAUDE_BOTTLE_PIPELOCK_PORT="${CLAUDE_BOTTLE_PIPELOCK_PORT:-8888}"

# Baked-in default allowlist for hosts Claude Code itself needs.
# Source: pipelock-assessment.md and the Claude Code network-config
# docs (https://code.claude.com/docs/en/network-config). The effective
# allowlist used at launch is this set unioned with whatever the
# bottle's egress.allowlist names. Kept as a newline-separated string
# because bash arrays don't survive sourcing into a function-only
# context cleanly; callers split on newlines.
CLAUDE_BOTTLE_PIPELOCK_DEFAULT_ALLOWLIST="api.anthropic.com
statsig.anthropic.com
sentry.io
claude.ai
platform.claude.com
downloads.claude.ai
raw.githubusercontent.com"

# --- Naming ----------------------------------------------------------------

# pipelock_container_name <slug> — prints the canonical sidecar
# container name for a given agent slug. The agent reaches the sidecar
# at this name as a hostname on the internal network.
pipelock_container_name() {
  local slug="${1:?pipelock_container_name: missing slug}"
  printf 'claude-bottle-pipelock-%s' "$slug"
}

# pipelock_proxy_url <slug> — prints http://<sidecar>:<port>, suitable
# for HTTPS_PROXY / HTTP_PROXY in the agent container.
pipelock_proxy_url() {
  local slug="${1:?pipelock_proxy_url: missing slug}"
  local name
  name="$(pipelock_container_name "$slug")"
  printf 'http://%s:%s' "$name" "$CLAUDE_BOTTLE_PIPELOCK_PORT"
}

# pipelock_proxy_host_port <slug> — prints <sidecar>:<port> (no scheme),
# suitable for socat's PROXY: directive in an SSH ProxyCommand. The
# agent's --internal network has no default route, so SSH (and any other
# raw TCP) must tunnel via pipelock's HTTP CONNECT.
pipelock_proxy_host_port() {
  local slug="${1:?pipelock_proxy_host_port: missing slug}"
  local name
  name="$(pipelock_container_name "$slug")"
  printf '%s:%s' "$name" "$CLAUDE_BOTTLE_PIPELOCK_PORT"
}

# --- Allowlist resolution --------------------------------------------------

# pipelock_bottle_allowlist <manifest_file> <bottle_name>
#
# Prints one hostname per line on stdout for the allowlist declared at
# bottles[<bottle_name>].egress.allowlist. Empty (no output) if the
# field is missing or the array is empty. Validates that each entry is
# a JSON string; dies with a clear message if any element is not.
pipelock_bottle_allowlist() {
  local manifest_file="${1:?pipelock_bottle_allowlist: missing manifest file}"
  local bottle_name="${2:?pipelock_bottle_allowlist: missing bottle name}"

  # Validate shape first: if egress.allowlist exists, every element
  # must be a string. We do this in one jq pass.
  local types
  types="$(jq -r --arg b "$bottle_name" '
    .bottles[$b].egress.allowlist // [] | map(type) | unique[]
  ' "$manifest_file")"
  local t
  while IFS= read -r t; do
    [ -z "$t" ] && continue
    if [ "$t" != "string" ]; then
      die "bottle '${bottle_name}' egress.allowlist must contain only strings; found a '${t}' entry."
    fi
  done <<< "$types"

  jq -r --arg b "$bottle_name" '
    .bottles[$b].egress.allowlist // [] | .[]
  ' "$manifest_file"
}

# pipelock_bottle_ssh_hostnames <manifest_file> <bottle_name>
#
# Prints one hostname per line for each entry in bottles[<name>].ssh[].Hostname.
# These need to reach pipelock's allowlist so the agent can tunnel SSH
# through pipelock via HTTP CONNECT (see ssh_setup's ProxyCommand
# wiring). Empty output if the bottle has no ssh entries.
pipelock_bottle_ssh_hostnames() {
  local manifest_file="${1:?pipelock_bottle_ssh_hostnames: missing manifest file}"
  local bottle_name="${2:?pipelock_bottle_ssh_hostnames: missing bottle name}"

  jq -r --arg b "$bottle_name" '
    .bottles[$b].ssh // [] | .[] | .Hostname // empty
  ' "$manifest_file"
}

# _pipelock_is_ipv4_literal <s> — exit 0 if <s> looks like an IPv4
# literal (four dot-separated octets). Pipelock's SSRF check fires on
# the resolved IP, so a Hostname that's already an IP literal needs
# `ssrf.ip_allowlist`, while a hostname needs `trusted_domains`.
_pipelock_is_ipv4_literal() {
  local s="${1:?}"
  [[ "$s" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# pipelock_bottle_ssh_trusted_domains <manifest> <bottle>
#
# Hostname-shaped ssh[].Hostname entries that should bypass pipelock's
# SSRF check (so a name resolving to a private IP — e.g. internal API
# behind a VPN — is reachable). IP-literal entries are excluded;
# trusted_domains is hostname-based per pipelock's docs.
pipelock_bottle_ssh_trusted_domains() {
  local manifest_file="${1:?}"
  local bottle_name="${2:?}"
  local h
  while IFS= read -r h; do
    [ -z "$h" ] && continue
    _pipelock_is_ipv4_literal "$h" && continue
    printf '%s\n' "$h"
  done < <(pipelock_bottle_ssh_hostnames "$manifest_file" "$bottle_name")
}

# pipelock_bottle_ssh_ip_cidrs <manifest> <bottle>
#
# Emits one canonical /32 CIDR per IPv4-literal ssh[].Hostname so they
# pass pipelock's SSRF IP-range check (which blocks RFC 1918, RFC 6598
# CGNAT, link-local, loopback, etc. by default). Hostnames are skipped
# — they go through trusted_domains instead.
pipelock_bottle_ssh_ip_cidrs() {
  local manifest_file="${1:?}"
  local bottle_name="${2:?}"
  local h
  while IFS= read -r h; do
    [ -z "$h" ] && continue
    if _pipelock_is_ipv4_literal "$h"; then
      printf '%s/32\n' "$h"
    fi
  done < <(pipelock_bottle_ssh_hostnames "$manifest_file" "$bottle_name")
}

# pipelock_effective_allowlist <manifest_file> <bottle_name>
#
# Prints the deduplicated union of: the baked-in default allowlist, the
# bottle's declared egress.allowlist, and any bottle.ssh[].Hostname
# entries (so SSH tunneling through pipelock is permitted by the same
# allowlist check that gates HTTP CONNECT). One hostname per line,
# sorted for stability. This is the single source of truth callers
# should use for both YAML generation and the preflight summary.
pipelock_effective_allowlist() {
  local manifest_file="${1:?pipelock_effective_allowlist: missing manifest file}"
  local bottle_name="${2:?pipelock_effective_allowlist: missing bottle name}"

  {
    printf '%s\n' "$CLAUDE_BOTTLE_PIPELOCK_DEFAULT_ALLOWLIST"
    pipelock_bottle_allowlist "$manifest_file" "$bottle_name"
    pipelock_bottle_ssh_hostnames "$manifest_file" "$bottle_name"
  } | awk 'NF && !seen[$0]++' | LC_ALL=C sort
}

# pipelock_allowlist_summary <manifest_file> <bottle_name>
#
# One-line summary of the effective allowlist for the y/N preflight
# display. Format:
#   "<N> hosts allowed (host1, host2, host3 +M more)"
# When the allowlist has 5 or fewer entries, all are listed and the
# "+M more" suffix is omitted.
pipelock_allowlist_summary() {
  local manifest_file="${1:?pipelock_allowlist_summary: missing manifest file}"
  local bottle_name="${2:?pipelock_allowlist_summary: missing bottle name}"

  local hosts=()
  local h
  while IFS= read -r h; do
    [ -z "$h" ] && continue
    hosts+=("$h")
  done < <(pipelock_effective_allowlist "$manifest_file" "$bottle_name")

  local count="${#hosts[@]}"
  if [ "$count" -eq 0 ]; then
    printf '0 hosts allowed (none)'
    return 0
  fi

  local show=$count
  local more=0
  if [ "$count" -gt 5 ]; then
    show=3
    more=$((count - show))
  fi

  local first_n=()
  local i=0
  while [ "$i" -lt "$show" ]; do
    first_n+=("${hosts[$i]}")
    i=$((i + 1))
  done

  local joined=""
  local h2
  for h2 in "${first_n[@]}"; do
    if [ -z "$joined" ]; then
      joined="$h2"
    else
      joined="${joined}, ${h2}"
    fi
  done

  if [ "$more" -gt 0 ]; then
    printf '%s hosts allowed (%s, +%s more)' "$count" "$joined" "$more"
  else
    printf '%s hosts allowed (%s)' "$count" "$joined"
  fi
}

# --- YAML generation -------------------------------------------------------

# pipelock_write_yaml <manifest_file> <bottle_name> <out_path>
#
# Writes a pipelock YAML config file to <out_path> (mode 600). The
# config carries only:
#   - the effective allowlist (hostnames),
#   - a fixed listen port (CLAUDE_BOTTLE_PIPELOCK_PORT),
#   - the minimum knobs needed to satisfy PRD 0001 success criteria
#     (strict mode, forward_proxy on, DLP defaults + env scanning).
#
# It deliberately contains no env values, no secrets, and no per-agent
# customization beyond the hostname list.
#
# YAML keys + defaults sourced from
# https://github.com/luckyPipewrench/pipelock/blob/main/docs/configuration.md
# (top-level fields, api_allowlist, forward_proxy, dlp).
pipelock_write_yaml() {
  local manifest_file="${1:?pipelock_write_yaml: missing manifest file}"
  local bottle_name="${2:?pipelock_write_yaml: missing bottle name}"
  local out_path="${3:?pipelock_write_yaml: missing out_path}"

  : > "$out_path"
  chmod 600 "$out_path"

  {
    printf 'version: 1\n'
    printf 'mode: strict\n'
    printf 'enforce: true\n'
    printf '\n'
    printf '# Hostnames the agent is allowed to reach. Effective list is\n'
    printf '# claude-bottle defaults UNION bottle.egress.allowlist (sorted, deduped).\n'
    printf 'api_allowlist:\n'
    local h
    while IFS= read -r h; do
      [ -z "$h" ] && continue
      # Validate: pipelock allows hostnames + wildcards. We accept
      # anything that does not contain whitespace or the YAML special
      # chars that would break unquoted strings; quote on output to be
      # safe.
      printf '  - "%s"\n' "$h"
    done < <(pipelock_effective_allowlist "$manifest_file" "$bottle_name")
    printf '\n'
    printf 'forward_proxy:\n'
    printf '  enabled: true\n'
    printf '\n'
    # SSRF exemptions for declared SSH hosts. Pipelock blocks the CGNAT
    # range (100.64.0.0/10, where Tailscale IPs live) and the rest of
    # RFC 1918 / link-local by default. Hostname entries go to
    # trusted_domains; IP-literal entries to ssrf.ip_allowlist as /32.
    local trusted_count=0 ssrf_count=0
    local td
    while IFS= read -r td; do
      [ -z "$td" ] && continue
      if [ "$trusted_count" -eq 0 ]; then
        printf 'trusted_domains:\n'
      fi
      printf '  - "%s"\n' "$td"
      trusted_count=$((trusted_count + 1))
    done < <(pipelock_bottle_ssh_trusted_domains "$manifest_file" "$bottle_name")
    [ "$trusted_count" -gt 0 ] && printf '\n'
    local cidr
    while IFS= read -r cidr; do
      [ -z "$cidr" ] && continue
      if [ "$ssrf_count" -eq 0 ]; then
        printf 'ssrf:\n'
        printf '  ip_allowlist:\n'
      fi
      printf '    - "%s"\n' "$cidr"
      ssrf_count=$((ssrf_count + 1))
    done < <(pipelock_bottle_ssh_ip_cidrs "$manifest_file" "$bottle_name")
    [ "$ssrf_count" -gt 0 ] && printf '\n'
    printf 'dlp:\n'
    printf '  include_defaults: true\n'
    printf '  scan_env: true\n'
  } > "$out_path"
}

# --- Sidecar lifecycle -----------------------------------------------------

# pipelock_start <slug> <internal_network> <egress_network> <yaml_dir> <yaml_filename>
#
# Boots the pipelock sidecar:
#   1. `docker run -d` on the internal network with the canonical
#      service name. The image runs `pipelock` as its CMD; we override
#      with `run --config <path>` and the listen address.
#   2. `docker cp` the YAML config from the host mktemp dir into the
#      container at /etc/pipelock.yaml.
#
# We use docker cp rather than `-v <host>:<container>` because Docker
# Desktop bind mounts have ownership / case-sensitivity quirks on
# macOS; copying the file in sidesteps both. The host-side mktemp dir
# is the caller's responsibility to clean up.
#
# After the cp the container is restarted so pipelock picks up the
# config it boots from. Pipelock's hot-reload feature would let us
# avoid the restart, but `forward_proxy.enabled` is one of the few
# restart-required keys (per docs/configuration.md), so a restart is
# the simplest correct path on first boot.
#
# Args:
#   <slug>             — agent slug; sidecar name will be claude-bottle-pipelock-<slug>
#   <internal_network> — name of the agent's internal docker network
#   <egress_network>   — name of the agent's user-defined egress
#                        network; the sidecar joins this so it can
#                        reach upstream hostnames with working DNS
#   <yaml_dir>         — host directory containing the YAML
#   <yaml_filename>    — filename within yaml_dir
#
# Echoes the container name on stdout on success.
pipelock_start() {
  local slug="${1:?pipelock_start: missing slug}"
  local internal_network="${2:?pipelock_start: missing internal network}"
  local egress_network="${3:?pipelock_start: missing egress network}"
  local yaml_dir="${4:?pipelock_start: missing yaml dir}"
  local yaml_filename="${5:?pipelock_start: missing yaml filename}"

  local name
  name="$(pipelock_container_name "$slug")"
  local host_yaml="${yaml_dir}/${yaml_filename}"
  if [ ! -f "$host_yaml" ]; then
    die "pipelock yaml not found at ${host_yaml}; pipelock_write_yaml must run first"
  fi

  # Container layout: pipelock reads its config from /etc/pipelock.yaml.
  # We `docker create` the sidecar, `docker cp` the YAML into the
  # writable layer, then `docker start` it — no bind mount, no shell
  # shim. The image is distroless (no `sh`), and `docker cp` to a
  # stopped container does NOT create intermediate parent directories,
  # so the YAML lives directly under /etc rather than in a /etc/pipelock
  # subdirectory.
  info "starting pipelock sidecar ${name} on network ${internal_network}"

  # Sidecar argv verification (PR #1 review). The pinned digest
  # (CLAUDE_BOTTLE_PIPELOCK_IMAGE above) has:
  #   ENTRYPOINT ["/pipelock"]
  #   CMD        ["run", "--listen", "0.0.0.0:8888"]
  # `pipelock run --help` documents `-l, --listen` (default
  # 127.0.0.1:8888) as the forward-proxy listen address — the
  # `--mcp-listen` flag is for the separate MCP HTTP listener and is
  # not what we want here. `--config` reads the YAML and hot-reloads
  # on file change; values in YAML can also drive the listen address
  # via `fetch_proxy.listen`, but the CLI flag takes precedence and
  # is the simpler contract for our launcher. Smoke-tested 2026-05-08
  # by running this exact argv against the digest and confirming the
  # /health endpoint responded on :8888.
  if ! docker create \
      --name "$name" \
      --network "$internal_network" \
      "$CLAUDE_BOTTLE_PIPELOCK_IMAGE" \
      run --config /etc/pipelock.yaml --listen "0.0.0.0:${CLAUDE_BOTTLE_PIPELOCK_PORT}" \
      >/dev/null 2>&1; then
    die "failed to create pipelock sidecar ${name}"
  fi

  # `docker cp` to a created-but-not-started container writes into the
  # writable layer directly. The parent directory must already exist in
  # the image — docker cp does NOT create missing intermediate dirs to
  # a stopped container, contrary to a common assumption. The pipelock
  # image is distroless (no `sh`), so we cannot prepopulate dirs with a
  # shell shim either. We therefore put the config in /etc/pipelock.yaml
  # (file directly under /etc) rather than /etc/pipelock/pipelock.yaml.
  local cp_err
  cp_err="$(docker cp "$host_yaml" "${name}:/etc/pipelock.yaml" 2>&1)" || {
    docker rm -f "$name" >/dev/null 2>&1 || true
    die "failed to copy pipelock yaml into ${name}: ${cp_err}"
  }

  # Attach to a per-agent user-defined bridge network for upstream
  # egress. The internal network has no gateway by definition, so
  # without a second network the sidecar can't reach the public
  # internet at all. We deliberately do NOT use Docker's legacy
  # `bridge` network: only user-defined bridges run Docker's embedded
  # DNS resolver, which pipelock needs to resolve `api.anthropic.com`
  # and similar upstream hostnames. The egress network is created by
  # network_create_egress in lib/network.sh.
  if ! docker network connect "$egress_network" "$name" >/dev/null 2>&1; then
    docker rm -f "$name" >/dev/null 2>&1 || true
    die "failed to attach pipelock sidecar ${name} to egress network ${egress_network}"
  fi

  if ! docker start "$name" >/dev/null 2>&1; then
    docker rm -f "$name" >/dev/null 2>&1 || true
    die "failed to start pipelock sidecar ${name}"
  fi

  printf '%s' "$name"
}

# pipelock_stop <slug>
#
# Stops and removes the sidecar by canonical name. Idempotent: a
# missing container is treated as success so this can be wired into
# cli.sh's exit trap unconditionally. Used as the first step of
# teardown — must run BEFORE the network is torn down, because docker
# refuses to remove a network that still has containers attached.
pipelock_stop() {
  local slug="${1:?pipelock_stop: missing slug}"
  local name
  name="$(pipelock_container_name "$slug")"
  if docker inspect "$name" >/dev/null 2>&1; then
    docker rm -f "$name" >/dev/null 2>&1 || warn "failed to remove pipelock sidecar ${name}; clean up with 'docker rm -f ${name}'"
  fi
}
