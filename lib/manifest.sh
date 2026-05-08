#!/usr/bin/env bash
# Manifest helpers. Read claude-bottle.json and pull the definition for a named
# agent.
#
# The manifest schema is documented in CLAUDE.md "Intended design". In
# short:
#   {
#     "boxes": {
#       "<box-name>": {
#         "env":     { "<NAME>": <env-entry>, ... },
#         "ssh":     [ <ssh-entry>, ... ]
#       },
#       ...
#     },
#     "agents": {
#       "<agent-name>": {
#         "skills":  [ "<skill-name>", ... ],
#         "prompt":  "<string>",
#         "box":     "<box-name>"
#       },
#       ...
#     }
#   }
#
# A box groups shared infrastructure (SSH keys, known hosts) that multiple
# agents can reference by name. The "box" field is required on every agent;
# cli.sh start rejects agents that omit it.
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
    # Merge: home is the base, cwd overrides on name conflict for both boxes and agents.
    jq -s '{
      "boxes":  ((.[0].boxes  // {}) * (.[1].boxes  // {})),
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
# on stdout (the keys of boxes[agent.box].env, in declaration order). No values.
# Prints nothing if the agent has no box or the box has no env.
manifest_env_names() {
  local manifest_file="${1:?manifest_env_names: missing manifest file}"
  local name="${2:?manifest_env_names: missing agent name}"
  jq -r --arg n "$name" '
    .agents[$n].box as $box |
    if ($box == null or $box == "") then empty
    else (.boxes[$box].env // {} | keys_unsorted[])
    end
  ' "$manifest_file"
}

# manifest_env_entry <manifest_file> <agent> <env_name> — prints the raw
# string value of a single env entry on stdout (no quoting, no JSON
# encoding). Env entries live on the agent's box (boxes[agent.box].env).
# Used by env_resolve.sh, which classifies the result by sentinel. Dies
# if the agent has no box, or the entry is not a JSON string; the
# prompt-at-runtime form is "?<message>", not JSON null.
manifest_env_entry() {
  local manifest_file="${1:?manifest_env_entry: missing manifest file}"
  local agent="${2:?manifest_env_entry: missing agent name}"
  local var="${3:?manifest_env_entry: missing env var name}"
  local box
  box="$(jq -r --arg a "$agent" '.agents[$a].box // ""' "$manifest_file")"
  if [ -z "$box" ]; then
    die "env entry ${var} for agent ${agent}: agent has no 'box' field"
  fi
  local entry_type
  entry_type="$(jq -r --arg b "$box" --arg v "$var" '.boxes[$b].env[$v] | type' "$manifest_file")"
  if [ "$entry_type" != "string" ]; then
    die "env entry ${var} for agent ${agent} must be a JSON string (was ${entry_type}). Use \"?<message>\" for prompt-at-runtime."
  fi
  jq -r --arg b "$box" --arg v "$var" '.boxes[$b].env[$v]' "$manifest_file"
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

# manifest_agent_box <manifest_file> <name> — prints the box name referenced
# by the agent on stdout, or an empty string if the agent has no "box" field.
manifest_agent_box() {
  local manifest_file="${1:?manifest_agent_box: missing manifest file}"
  local name="${2:?manifest_agent_box: missing agent name}"
  jq -r --arg n "$name" '.agents[$n].box // ""' "$manifest_file"
}

# manifest_has_box <manifest_file> <box_name> — returns 0 if the named box
# exists in the manifest, else 1.
manifest_has_box() {
  local manifest_file="${1:?manifest_has_box: missing manifest file}"
  local box_name="${2:?manifest_has_box: missing box name}"
  jq -e --arg b "$box_name" '.boxes | has($b)' "$manifest_file" >/dev/null 2>&1
}

# manifest_require_box <manifest_file> <box_name> — like manifest_has_box but
# dies with a useful message (and prints available box names) if the box is
# not defined.
manifest_require_box() {
  local manifest_file="${1:?manifest_require_box: missing manifest file}"
  local box_name="${2:?manifest_require_box: missing box name}"
  if ! manifest_has_box "$manifest_file" "$box_name"; then
    local available
    available="$(jq -r '.boxes // {} | keys_unsorted | join(", ")' "$manifest_file" 2>/dev/null || echo "")"
    if [ -n "$available" ]; then
      die "box '${box_name}' not defined in claude-bottle.json. Available boxes: ${available}"
    else
      die "box '${box_name}' not defined in claude-bottle.json (no boxes defined)."
    fi
  fi
}

# manifest_box_ssh <manifest_file> <box_name> — prints one compact JSON object
# per line for each ssh entry in boxes[box_name].ssh. Prints nothing if the
# box has no ssh array or it is empty.
manifest_box_ssh() {
  local manifest_file="${1:?manifest_box_ssh: missing manifest file}"
  local box_name="${2:?manifest_box_ssh: missing box name}"
  jq -c --arg b "$box_name" '.boxes[$b].ssh // [] | .[]' "$manifest_file"
}

# manifest_ssh <manifest_file> <name> — prints one compact JSON object per line
# for each ssh entry associated with the agent. SSH entries are resolved via
# the agent's "box" field: if set, entries come from boxes[box].ssh; if the
# agent has no "box" field, prints nothing.
# Each object has: Host, IdentityFile, Hostname, User, Port (required);
# KnownHostKey (optional).
manifest_ssh() {
  local manifest_file="${1:?manifest_ssh: missing manifest file}"
  local name="${2:?manifest_ssh: missing agent name}"
  local box
  box="$(jq -r --arg n "$name" '.agents[$n].box // ""' "$manifest_file")"
  if [ -z "$box" ]; then
    return 0
  fi
  jq -c --arg b "$box" '.boxes[$b].ssh // [] | .[]' "$manifest_file"
}
