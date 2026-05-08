#!/usr/bin/env bash
# Manifest helpers. Read claude-bottle.json and pull the definition for a named
# agent.
#
# The manifest schema is documented in CLAUDE.md "Intended design". In
# short:
#   {
#     "bottles": {
#       "<bottle-name>": {
#         "env":     { "<NAME>": <env-entry>, ... },
#         "ssh":     [ <ssh-entry>, ... ],
#         "egress":  { "allowlist": [ "<hostname>", ... ] }
#       },
#       ...
#     },
#     "agents": {
#       "<agent-name>": {
#         "skills":  [ "<skill-name>", ... ],
#         "prompt":  "<string>",
#         "bottle":     "<bottle-name>"
#       },
#       ...
#     }
#   }
#
# A bottle groups shared infrastructure (SSH keys, known hosts, egress
# allowlist) that multiple agents can reference by name. The "bottle" field
# is required on every agent; cli.sh start rejects agents that omit it.
#
# The "egress" object is added in PRD 0001. Today it carries one key:
#   - allowlist: array of hostnames the agent is allowed to reach. The
#     effective allowlist at launch is this list UNIONED with the
#     baked-in defaults for Claude Code's required hosts (see
#     lib/pipelock.sh). Bottles with no "egress" block use defaults
#     only. Future keys (mode, dlp, data_budget, ...) are reserved
#     under the same object; v1 ignores anything we don't recognize.
#
# An <env-entry> is a JSON string. Mode is selected by sentinel prefix:
#   "?<message>"   → prompt for the value at runtime, displaying <message>
#                    (bare "?" is allowed; uses a default prompt)
#   "${HOST_VAR}"  → interpolate from $HOST_VAR in the host process env
#   any other str  → literal (the JSON string is the value verbatim)
# The classification lives in env_resolve.sh (env_entry_kind); this
# module only fetches the raw string and validates that it is a string.
#
# Manifest parsing happens on the host with `jq`, never inside the
# container. We never echo env *values* here — only names. For literal
# entries the "name" and the value happen to be the same shape (both
# are JSON strings), so callers must take care not to log the result of
# manifest_env_entry.
#
# All functions (except manifest_resolve) take a manifest_file argument —
# the path to a resolved JSON file, typically produced by manifest_resolve.
#
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_MANIFEST_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_MANIFEST_SOURCED=1

_iso_lib_manifest_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_manifest_dir}/log.sh"

# require_jq — fails with an install pointer if `jq` is not on PATH.
require_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    info "jq is required on the host for claude-bottle manifest parsing."
    info "macOS:  brew install jq"
    info "Linux:  apt-get install jq  (or your distro equivalent)"
    die "jq not found"
  fi
}

# manifest_resolve <cwd> — looks for claude-bottle.json in <cwd> and in $HOME,
# merges the two (cwd entries override home entries for the same agent name),
# and prints the merged JSON to stdout. Dies if neither file is found or if
# either found file is not valid JSON.
manifest_resolve() {
  local cwd="${1:?manifest_resolve: missing cwd}"
  local cwd_file="${cwd}/claude-bottle.json"
  local home_file="${HOME}/claude-bottle.json"

  local has_cwd=0 has_home=0

  if [ -f "$cwd_file" ]; then
    if ! jq -e . "$cwd_file" >/dev/null 2>&1; then
      die "claude-bottle.json at ${cwd_file} is not valid JSON"
    fi
    has_cwd=1
  fi

  if [ -f "$home_file" ]; then
    if ! jq -e . "$home_file" >/dev/null 2>&1; then
      die "claude-bottle.json at ${home_file} is not valid JSON"
    fi
    has_home=1
  fi

  if [ "$has_cwd" = "0" ] && [ "$has_home" = "0" ]; then
    die "no claude-bottle.json found in ${cwd} or ${HOME}"
  elif [ "$has_cwd" = "1" ] && [ "$has_home" = "0" ]; then
    cat "$cwd_file"
  elif [ "$has_cwd" = "0" ] && [ "$has_home" = "1" ]; then
    cat "$home_file"
  else
    # Merge: home is the base, cwd overrides on name conflict for both bottles and agents.
    jq -s '{
      "bottles":  ((.[0].bottles  // {}) * (.[1].bottles  // {})),
      "agents": ((.[0].agents // {}) * (.[1].agents // {}))
    }' "$home_file" "$cwd_file"
  fi
}

# manifest_has_agent <manifest_file> <name> — returns 0 if the agent key
# exists in the manifest, else 1.
manifest_has_agent() {
  local manifest_file="${1:?manifest_has_agent: missing manifest file}"
  local name="${2:?manifest_has_agent: missing agent name}"
  jq -e --arg n "$name" '.agents | has($n)' "$manifest_file" >/dev/null 2>&1
}

# manifest_require_agent <manifest_file> <name> — like manifest_has_agent but
# dies with a useful message (and prints the available agent names) if the
# named agent is not defined.
manifest_require_agent() {
  local manifest_file="${1:?manifest_require_agent: missing manifest file}"
  local name="${2:?manifest_require_agent: missing agent name}"
  if ! manifest_has_agent "$manifest_file" "$name"; then
    local available
    available="$(jq -r '.agents | keys_unsorted | join(", ")' "$manifest_file" 2>/dev/null || echo "")"
    if [ -n "$available" ]; then
      die "agent '${name}' not defined in claude-bottle.json. Available: ${available}"
    else
      die "agent '${name}' not defined in claude-bottle.json (manifest is empty)."
    fi
  fi
}

# manifest_env_names <manifest_file> <name> — prints one env-var name per line
# on stdout (the keys of bottles[agent.bottle].env, in declaration order). No values.
# Prints nothing if the agent has no bottle or the bottle has no env.
manifest_env_names() {
  local manifest_file="${1:?manifest_env_names: missing manifest file}"
  local name="${2:?manifest_env_names: missing agent name}"
  jq -r --arg n "$name" '
    .agents[$n].bottle as $bottle |
    if ($bottle == null or $bottle == "") then empty
    else (.bottles[$bottle].env // {} | keys_unsorted[])
    end
  ' "$manifest_file"
}

# manifest_env_entry <manifest_file> <agent> <env_name> — prints the raw
# string value of a single env entry on stdout (no quoting, no JSON
# encoding). Env entries live on the agent's bottle (bottles[agent.bottle].env).
# Used by env_resolve.sh, which classifies the result by sentinel. Dies
# if the agent has no bottle, or the entry is not a JSON string; the
# prompt-at-runtime form is "?<message>", not JSON null.
manifest_env_entry() {
  local manifest_file="${1:?manifest_env_entry: missing manifest file}"
  local agent="${2:?manifest_env_entry: missing agent name}"
  local var="${3:?manifest_env_entry: missing env var name}"
  local bottle
  bottle="$(jq -r --arg a "$agent" '.agents[$a].bottle // ""' "$manifest_file")"
  if [ -z "$bottle" ]; then
    die "env entry ${var} for agent ${agent}: agent has no 'bottle' field"
  fi
  local entry_type
  entry_type="$(jq -r --arg b "$bottle" --arg v "$var" '.bottles[$b].env[$v] | type' "$manifest_file")"
  if [ "$entry_type" != "string" ]; then
    die "env entry ${var} for agent ${agent} must be a JSON string (was ${entry_type}). Use \"?<message>\" for prompt-at-runtime."
  fi
  jq -r --arg b "$bottle" --arg v "$var" '.bottles[$b].env[$v]' "$manifest_file"
}

# manifest_skills <manifest_file> <name> — prints one skill name per line on
# stdout (the elements of agent.skills, in order).
manifest_skills() {
  local manifest_file="${1:?manifest_skills: missing manifest file}"
  local name="${2:?manifest_skills: missing agent name}"
  jq -r --arg n "$name" '.agents[$n].skills // [] | .[]' "$manifest_file"
}

# manifest_prompt <manifest_file> <name> — prints the prompt string on stdout
# (no trailing newline manipulation; the raw value goes out). Empty string
# if not set.
manifest_prompt() {
  local manifest_file="${1:?manifest_prompt: missing manifest file}"
  local name="${2:?manifest_prompt: missing agent name}"
  jq -r --arg n "$name" '.agents[$n].prompt // ""' "$manifest_file"
}

# manifest_agent_bottle <manifest_file> <name> — prints the bottle name referenced
# by the agent on stdout, or an empty string if the agent has no "bottle" field.
manifest_agent_bottle() {
  local manifest_file="${1:?manifest_agent_bottle: missing manifest file}"
  local name="${2:?manifest_agent_bottle: missing agent name}"
  jq -r --arg n "$name" '.agents[$n].bottle // ""' "$manifest_file"
}

# manifest_has_bottle <manifest_file> <bottle_name> — returns 0 if the named bottle
# exists in the manifest, else 1.
manifest_has_bottle() {
  local manifest_file="${1:?manifest_has_bottle: missing manifest file}"
  local bottle_name="${2:?manifest_has_bottle: missing bottle name}"
  jq -e --arg b "$bottle_name" '.bottles | has($b)' "$manifest_file" >/dev/null 2>&1
}

# manifest_require_bottle <manifest_file> <bottle_name> — like manifest_has_bottle but
# dies with a useful message (and prints available bottle names) if the bottle is
# not defined.
manifest_require_bottle() {
  local manifest_file="${1:?manifest_require_bottle: missing manifest file}"
  local bottle_name="${2:?manifest_require_bottle: missing bottle name}"
  if ! manifest_has_bottle "$manifest_file" "$bottle_name"; then
    local available
    available="$(jq -r '.bottles // {} | keys_unsorted | join(", ")' "$manifest_file" 2>/dev/null || echo "")"
    if [ -n "$available" ]; then
      die "bottle '${bottle_name}' not defined in claude-bottle.json. Available bottles: ${available}"
    else
      die "bottle '${bottle_name}' not defined in claude-bottle.json (no bottles defined)."
    fi
  fi
}

# manifest_bottle_ssh <manifest_file> <bottle_name> — prints one compact JSON object
# per line for each ssh entry in bottles[bottle_name].ssh. Prints nothing if the
# bottle has no ssh array or it is empty.
manifest_bottle_ssh() {
  local manifest_file="${1:?manifest_bottle_ssh: missing manifest file}"
  local bottle_name="${2:?manifest_bottle_ssh: missing bottle name}"
  jq -c --arg b "$bottle_name" '.bottles[$b].ssh // [] | .[]' "$manifest_file"
}

# manifest_bottle_egress_allowlist <manifest_file> <bottle_name> — prints one
# hostname per line on stdout for the entries in
# bottles[bottle_name].egress.allowlist. Prints nothing if the field is missing
# or the array is empty. Validates only that the field, when present, is an
# array; per-element string typing is checked at use-time in lib/pipelock.sh
# so the validation lives next to the YAML generator that consumes it.
manifest_bottle_egress_allowlist() {
  local manifest_file="${1:?manifest_bottle_egress_allowlist: missing manifest file}"
  local bottle_name="${2:?manifest_bottle_egress_allowlist: missing bottle name}"
  local field_type
  field_type="$(jq -r --arg b "$bottle_name" '.bottles[$b].egress.allowlist | type' "$manifest_file" 2>/dev/null || echo "null")"
  case "$field_type" in
    array|null) : ;;
    *) die "bottle '${bottle_name}' egress.allowlist must be an array (was ${field_type})." ;;
  esac
  jq -r --arg b "$bottle_name" '.bottles[$b].egress.allowlist // [] | .[]' "$manifest_file"
}

# manifest_ssh <manifest_file> <name> — prints one compact JSON object per line
# for each ssh entry associated with the agent. SSH entries are resolved via
# the agent's "bottle" field: if set, entries come from bottles[bottle].ssh; if the
# agent has no "bottle" field, prints nothing.
# Each object has: Host, IdentityFile, Hostname, User, Port (required);
# KnownHostKey (optional).
manifest_ssh() {
  local manifest_file="${1:?manifest_ssh: missing manifest file}"
  local name="${2:?manifest_ssh: missing agent name}"
  local bottle
  bottle="$(jq -r --arg n "$name" '.agents[$n].bottle // ""' "$manifest_file")"
  if [ -z "$bottle" ]; then
    return 0
  fi
  jq -c --arg b "$bottle" '.bottles[$b].ssh // [] | .[]' "$manifest_file"
}
