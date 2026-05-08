#!/usr/bin/env bash
# Skill copier. Copies named skills from the host's ~/.claude/skills/<name>/
# into the running container's ~/.claude/skills/<name>/, preserving
# directory structure (no flattening, no archives), per CLAUDE.md
# "Intended design".
#
# Scope of THIS file (matches PRD 0002 "Open question 3" resolution):
#   - host → container only.
#   - if a referenced skill is missing on the host, fail with a clear
#     message naming the skill. No silent skipping. The repo-side
#     `skills/<name>/` snapshot and host↔repo diff prompt described in
#     CLAUDE.md "Intended design" are deferred.
#
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_SKILLS_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_SKILLS_SOURCED=1

_iso_lib_skills_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_skills_dir}/log.sh"

# Container-side home/skills paths. The Dockerfile sets the user to `node`
# (uid 1000) with home /home/node, so this is where claude-code looks.
CLAUDE_BOTTLE_CONTAINER_HOME="${CLAUDE_BOTTLE_CONTAINER_HOME:-/home/node}"
CLAUDE_BOTTLE_CONTAINER_SKILLS_DIR="${CLAUDE_BOTTLE_CONTAINER_SKILLS_DIR:-${CLAUDE_BOTTLE_CONTAINER_HOME}/.claude/skills}"

# host_skill_dir <name> — prints the absolute host path for a skill.
host_skill_dir() {
  local name="${1:?host_skill_dir: missing skill name}"
  printf '%s/.claude/skills/%s' "${HOME:?HOME not set}" "$name"
}

# host_skill_exists <name> — returns 0 if the host has a skill directory
# at ~/.claude/skills/<name>/, else 1.
host_skill_exists() {
  local name="${1:?host_skill_exists: missing skill name}"
  [ -d "$(host_skill_dir "$name")" ]
}

# require_host_skill <name> — dies with a clear message if the named
# skill is missing on the host. The error names the skill and the path
# checked.
require_host_skill() {
  local name="${1:?require_host_skill: missing skill name}"
  if ! host_skill_exists "$name"; then
    die "skill '${name}' not found on host at $(host_skill_dir "$name"). Create it under ~/.claude/skills/, then re-run."
  fi
}

# skills_validate_all <name1> [<name2> ...] — checks every named skill
# exists on the host, dies on the first one that does not. No copy yet.
# Use this BEFORE the confirmation prompt so the user does not get
# asked y/N for a plan that's already known to fail.
skills_validate_all() {
  local n
  for n in "$@"; do
    require_host_skill "$n"
  done
}

# skills_copy_into <container> <name1> [<name2> ...]
#
# For each named skill:
#   1. ensure ~/.claude/skills/ exists in the container (mkdir -p)
#   2. `docker cp <host_skill_dir>/. <container>:<container_skills>/<name>/`
#      — the trailing `/.` on the source preserves directory structure
#      and copies the contents into a freshly-created destination dir,
#      avoiding the docker-cp quirk where copying `dir` (no slash) into
#      an existing `dest/` would nest as `dest/dir/`.
#
# The destination directory is removed first if it already exists, so
# repeated calls produce a deterministic state.
skills_copy_into() {
  local container="${1:?skills_copy_into: missing container name}"
  shift
  if [ "$#" -eq 0 ]; then
    return 0
  fi

  # Ensure the target parent dir exists in the container. This is a
  # no-op if the Dockerfile already created it, but cheap and defensive.
  docker exec "$container" mkdir -p "${CLAUDE_BOTTLE_CONTAINER_SKILLS_DIR}" >/dev/null

  local n src dst
  for n in "$@"; do
    src="$(host_skill_dir "$n")"
    if [ ! -d "$src" ]; then
      die "skill '${n}' disappeared from host between validation and copy at ${src}."
    fi
    dst="${CLAUDE_BOTTLE_CONTAINER_SKILLS_DIR}/${n}"
    info "copying skill ${n} into ${container}:${dst}"
    # Wipe any prior copy so we're deterministic, then create empty dst
    # and copy contents-of-src into it via the `/.` source-suffix trick.
    docker exec "$container" rm -rf "$dst" >/dev/null
    docker exec "$container" mkdir -p "$dst" >/dev/null
    docker cp "${src}/." "${container}:${dst}/" >/dev/null
  done
}
