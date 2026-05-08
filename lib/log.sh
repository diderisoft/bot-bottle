#!/usr/bin/env bash
# Tiny logging wrappers. Sourced by entry-point scripts.
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_LOG_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_LOG_SOURCED=1

# info <msg...> — informational message to stderr.
info() {
  printf 'claude-bottle: %s\n' "$*" >&2
}

# warn <msg...> — warning to stderr.
warn() {
  printf 'claude-bottle: warning: %s\n' "$*" >&2
}

# die <msg...> — error to stderr, exit 1.
die() {
  printf 'claude-bottle: error: %s\n' "$*" >&2
  exit 1
}
