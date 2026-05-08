#!/usr/bin/env bash
# Docker helpers. Build/inspect primitives shared by cli.sh
# (and reusable by future skill-sync / secret-injection scripts).
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_DOCKER_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_DOCKER_SOURCED=1

_iso_lib_docker_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_docker_dir}/log.sh"

# require_docker — fails with an install pointer if `docker` is not on PATH.
require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    info "Docker is required but was not found on PATH."
    info "macOS:  install Docker Desktop  https://docs.docker.com/desktop/install/mac-install/"
    info "Linux:  install Docker Engine   https://docs.docker.com/engine/install/"
    die "docker not found"
  fi
}

# image_exists <ref> — returns 0 if the named local image exists, else 1.
image_exists() {
  local ref="${1:?image_exists: missing image reference}"
  docker image inspect "$ref" >/dev/null 2>&1
}

# container_exists <name> — returns 0 if a container (running or stopped)
# with the given name exists, else 1.
container_exists() {
  local name="${1:?container_exists: missing container name}"
  # `docker ps -a -q -f name=^<name>$` prints the container id if it exists.
  local id
  id="$(docker ps -a -q -f "name=^${name}$" 2>/dev/null || true)"
  [ -n "$id" ]
}

# slugify <name> — prints a DNS-safe slug (lowercase, non-alnum runs → '-',
# trimmed) on stdout. Exits non-zero if the result is empty.
slugify() {
  local input="${1:?slugify: missing name}"
  local slug
  slug="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
  if [ -z "$slug" ]; then
    die "name '${input}' produced an empty slug; use alphanumeric characters"
  fi
  printf '%s' "$slug"
}

# build_image <ref> <context_dir> — invokes `docker build` every call. The
# layer cache makes no-change rebuilds cheap (typically <1s); always running
# the build means edits to the Dockerfile (or anything COPY'd in) take
# effect on the next cli.sh without the user having to manually `docker
# rmi` first.
build_image() {
  local ref="${1:?build_image: missing image reference}"
  local context="${2:?build_image: missing build context directory}"

  info "building image ${ref} from ${context} (layer cache keeps repeat builds fast)"
  docker build -t "$ref" "$context"
}

# build_image_with_cwd <derived_ref> <base_ref> <cwd>
#
# Builds a thin derived image that copies the contents of <cwd> into
# /home/node/workspace (owned by node:node) and sets WORKDIR there, so
# the launched claude session starts inside the user's project.
#
# The Dockerfile is piped via stdin (`-f -`) so no file is written into
# <cwd> — only the build context is read from there. Any .dockerignore
# already in <cwd> is honored automatically by docker build.
#
# A trust-dialog entry for /home/node/workspace is added to
# ~/.claude.json during the build, because the baked-in entry in the
# base image only covers /home/node and claude's "trust this folder"
# prompt is keyed on cwd.
build_image_with_cwd() {
  local derived="${1:?build_image_with_cwd: missing derived ref}"
  local base="${2:?build_image_with_cwd: missing base ref}"
  local cwd="${3:?build_image_with_cwd: missing cwd}"

  if [ ! -d "$cwd" ]; then
    die "cwd not found at ${cwd}"
  fi

  info "building image ${derived} from ${base} with ${cwd} -> /home/node/workspace"
  docker build -t "$derived" -f - "$cwd" <<DOCKERFILE
FROM ${base}
COPY --chown=node:node . /home/node/workspace
RUN node -e 'const fs=require("fs"),p=process.env.HOME+"/.claude.json",c=JSON.parse(fs.readFileSync(p,"utf8"));c.projects=c.projects||{};c.projects[process.env.HOME+"/workspace"]={hasTrustDialogAccepted:true};fs.writeFileSync(p,JSON.stringify(c,null,2));'
WORKDIR /home/node/workspace
DOCKERFILE
}
