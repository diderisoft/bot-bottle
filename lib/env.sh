#!/usr/bin/env bash
# Env-var helpers. Set/unset checks only — never echo a secret value.
# See CLAUDE.md "Checking env vars safely" for the rule this enforces.
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_ENV_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_ENV_SOURCED=1

# Resolve sibling helpers regardless of caller's cwd.
_iso_lib_env_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_env_dir}/log.sh"

# require_env <NAME> — fails with a clear message if the named env var is
# unset or empty. Crucially does NOT print the value, the length, or any
# substring; only the variable name is echoed.
#
# Usage:
#   require_env ANTHROPIC_API_KEY
require_env() {
  local name="${1:-}"
  if [ -z "$name" ]; then
    die "require_env: missing variable name argument"
  fi

  # Indirect expansion to read the named variable without naming it twice.
  local value="${!name-}"
  if [ -z "$value" ]; then
    die "required env var ${name} is not set. Export it in your shell and re-run."
  fi
}
