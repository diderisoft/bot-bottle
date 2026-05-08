#!/usr/bin/env bash
# cli.sh — manage claude-bottle containers.
#
# usage: cli.sh <command> [args...]
#
# Commands:
#   build     build (or rebuild) the claude-bottle Docker image.
#   cleanup   stop and remove all active claude-bottle containers.
#   info      print env, skills, and prompt details for a named agent.
#   list      list available agents or active containers.
#   start     boot a sandboxed container for a named agent and attach an
#             interactive claude-code session. The container is torn down
#             when the session ends.

set -euo pipefail

# Capture the user's cwd before anything else touches it.
USER_CWD="${PWD}"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR"

# shellcheck source=lib/log.sh
. "${SCRIPT_DIR}/lib/log.sh"
# shellcheck source=lib/docker.sh
. "${SCRIPT_DIR}/lib/docker.sh"
# shellcheck source=lib/env.sh
. "${SCRIPT_DIR}/lib/env.sh"
# shellcheck source=lib/manifest.sh
. "${SCRIPT_DIR}/lib/manifest.sh"
# shellcheck source=lib/env_resolve.sh
. "${SCRIPT_DIR}/lib/env_resolve.sh"
# shellcheck source=lib/skills.sh
. "${SCRIPT_DIR}/lib/skills.sh"
# shellcheck source=lib/ssh.sh
. "${SCRIPT_DIR}/lib/ssh.sh"

usage() {
  printf 'usage: %s <command> [args...]\n' "$(basename "$0")" >&2
  printf '\n' >&2
  printf 'Commands:\n' >&2
  printf '  build     build (or rebuild) the claude-bottle Docker image\n' >&2
  printf '  cleanup   stop and remove all active claude-bottle containers\n' >&2
  printf '  edit      open an agent in vim for editing\n' >&2
  printf '  info      print env, skills, and prompt details for a named agent\n' >&2
  printf '  init      interactively create a new agent and add it to claude-bottle.json\n' >&2
  printf '  list      list available agents or active containers\n' >&2
  printf '  start     boot a container for a named agent and attach an interactive session\n' >&2
  printf '\n' >&2
  printf "Run '%s <command> --help' for command-specific usage.\n" "$(basename "$0")" >&2
}

cmd_build() {
  require_docker
  build_image "${CLAUDE_BOTTLE_IMAGE:-claude-bottle:latest}" "$REPO_DIR"
}

cmd_info() {
  usage_info() {
    printf 'usage: %s info <name>\n' "$(basename "$0")" >&2
    printf '  <name> must be defined in claude-bottle.json at the repo root.\n' >&2
  }

  if [ "$#" -lt 1 ]; then
    usage_info
    exit 2
  fi

  case "$1" in
    -h|--help) usage_info; exit 0 ;;
    -*) usage_info; die "unknown flag: $1" ;;
  esac

  local NAME="$1"
  require_jq
  local MANIFEST_FILE
  MANIFEST_FILE="$(mktemp -t claude-bottle-manifest.XXXXXX.json)"
  trap 'rm -f "${MANIFEST_FILE:-}"' EXIT
  manifest_resolve "$USER_CWD" > "$MANIFEST_FILE"
  manifest_require_agent "$MANIFEST_FILE" "$NAME"

  local env_names="" _en
  while IFS= read -r _en; do
    [ -z "$_en" ] && continue
    env_names="${env_names:+${env_names}, }${_en}"
  done < <(manifest_env_names "$MANIFEST_FILE" "$NAME")

  local skill_names=() _sk
  while IFS= read -r _sk; do
    [ -z "$_sk" ] && continue
    skill_names+=("$_sk")
  done < <(manifest_skills "$MANIFEST_FILE" "$NAME")

  local prompt_content prompt_len prompt_first_line
  prompt_content="$(manifest_prompt "$MANIFEST_FILE" "$NAME")"
  prompt_len="${#prompt_content}"
  prompt_first_line="$(printf '%s' "$prompt_content" | awk 'NR==1{print; exit}')"

  local box_name
  box_name="$(manifest_agent_box "$MANIFEST_FILE" "$NAME")"

  local ssh_entries=() _se
  while IFS= read -r _se; do
    [ -z "$_se" ] && continue
    ssh_entries+=("$_se")
  done < <(manifest_ssh "$MANIFEST_FILE" "$NAME")

  printf '\n'
  info "agent           : ${NAME}"
  info "env (names only): ${env_names:-(none)}"
  info "skills          : ${skill_names[*]:-(none)}"
  info "prompt          : ${prompt_len} chars; first line: ${prompt_first_line:-(empty)}"
  if [ -n "$box_name" ]; then
    info "box             : ${box_name}"
    if [ "${#ssh_entries[@]}" -gt 0 ]; then
      local _n _h _u _p _k _khk
      for _se in "${ssh_entries[@]}"; do
        _n="$(printf '%s' "$_se" | jq -r '.Host')"
        _h="$(printf '%s' "$_se" | jq -r '.Hostname')"
        _u="$(printf '%s' "$_se" | jq -r '.User')"
        _p="$(printf '%s' "$_se" | jq -r '.Port')"
        _k="$(printf '%s' "$_se" | jq -r '.IdentityFile')"
        _khk="$(printf '%s' "$_se" | jq -r '.KnownHostKey // empty')"
        info "  ssh host      : ${_n} (Hostname=${_h}, User=${_u}, Port=${_p}, IdentityFile=${_k})"
        [ -n "$_khk" ] && info "    KnownHostKey: ${_khk}"
      done
    else
      info "  ssh hosts     : (none)"
    fi
  else
    info "box             : (none)"
  fi
  printf '\n'
}

cmd_list() {
  usage_list() {
    printf 'usage: %s list <available|active>\n' "$(basename "$0")" >&2
    printf '  available   list agent names defined in claude-bottle.json\n' >&2
    printf '  active      list running claude-bottle containers\n' >&2
  }

  if [ "$#" -lt 1 ]; then
    usage_list
    exit 2
  fi

  case "$1" in
    available)
      require_jq
      local MANIFEST_FILE
      MANIFEST_FILE="$(mktemp -t claude-bottle-manifest.XXXXXX.json)"
      trap 'rm -f "${MANIFEST_FILE:-}"' EXIT
      manifest_resolve "$USER_CWD" > "$MANIFEST_FILE"
      jq -r '.agents | keys_unsorted[]' "$MANIFEST_FILE"
      ;;
    active)
      require_docker
      local containers
      containers="$(docker ps --filter 'name=^claude-bottle-' --format '{{.Names}}{{"\t"}}{{.Status}}' 2>/dev/null || true)"
      if [ -z "$containers" ]; then
        info "no active claude-bottle containers"
        return 0
      fi
      printf '\n'
      local name status
      while IFS=$'\t' read -r name status; do
        info "container: ${name}  status: ${status}"
      done <<< "$containers"
      printf '\n'
      ;;
    -h|--help) usage_list; exit 0 ;;
    *) usage_list; die "unknown argument: $1" ;;
  esac
}

cmd_cleanup() {
  require_docker
  local containers
  containers="$(docker ps --filter 'name=^claude-bottle-' --format '{{.Names}}' 2>/dev/null || true)"
  if [ -z "$containers" ]; then
    info "no active claude-bottle containers"
    return 0
  fi
  printf '\n' >&2
  local name
  while IFS= read -r name; do
    info "found: ${name}"
  done <<< "$containers"
  printf '\n' >&2
  printf 'claude-bottle: remove all of the above? [y/N] ' >&2
  local REPLY
  IFS= read -r REPLY </dev/tty
  case "$REPLY" in
    y|Y|yes|YES) ;;
    *) info "aborted"; return 0 ;;
  esac
  while IFS= read -r name; do
    info "removing ${name}"
    docker rm -f "$name" >/dev/null
  done <<< "$containers"
  info "done"
}

# ---------------------------------------------------------------------------
# cmd_start — bring up an ephemeral claude-bottle container configured for a
# named agent from the repo-root claude-bottle.json manifest, and drop the
# user into an interactive claude-code session inside it.
#
# Lifecycle (per PRD 0001 "ephemeral" requirement): the container is
# removed automatically when the interactive session ends. We use
# `docker run --rm -d` plus a trap that forces removal on exit, so
# signals like Ctrl-C also clean up.
#
# ASSUMPTION: the container is started detached (`-d`) running `sleep
# infinity` so that skills and config can be copied in via `docker cp`
# before `docker exec` attaches the claude session. The container therefore
# stays alive in the background between launch and attach — the EXIT/INT/TERM
# trap is what guarantees teardown on normal exit. SIGKILL bypasses the
# trap; if this process is killed that way the container will be left
# running and must be removed manually with `docker rm -f <name>`.
#
# Per-agent configuration (PRD 0002):
#   - env vars in three modes (secret-prompted, literal, interpolated
#     from the host process env). Resolved by lib/env_resolve.sh.
#       * secret      → prompted from /dev/tty, exported, forwarded via
#                       `docker run -e NAME` (no `=value`).
#       * interpolated→ copied from a host var into this process under
#                       the target name, forwarded the same way as a
#                       secret (off argv, off disk).
#       * literal     → written to a mode-600 env-file under mktemp -d
#                       and forwarded with `--env-file <path>`.
#   - skills: host directories under ~/.claude/skills/<name>/ are
#     `docker cp`'d into the running container's
#     ~/.claude/skills/<name>/ by lib/skills.sh.
#   - prompt: written to a host-side mode-600 file, then `docker cp`'d
#     into the container (so the prompt content never lands on
#     `docker exec` argv) and passed to
#     `claude --append-system-prompt-file <path>`.
#
# Confirmation: the resolved plan (skill names, env var names — never
# values, prompt length and first line) is shown before launch and
# gated on a single y/N.
#
# Dry-run: pass --dry-run (or set CLAUDE_BOTTLE_DRY_RUN=1) to print the
# resolved plan and exit BEFORE docker run / cp / exec. Used for
# verifying the manifest wiring without booting Claude.
# ---------------------------------------------------------------------------
cmd_start() {
  usage_start() {
    printf 'usage: %s start [--dry-run] [--cwd] <name>\n' "$(basename "$0")" >&2
    printf '  <name> must be defined in claude-bottle.json at the repo root.\n' >&2
    printf '  --cwd         copy the current working directory into a derived image at\n' >&2
    printf '                /home/node/workspace and start claude there.\n' >&2
  }

  local DRY_RUN="${CLAUDE_BOTTLE_DRY_RUN:-0}"
  local COPY_CWD=0
  local NAME=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --cwd) COPY_CWD=1; shift ;;
      -h|--help) usage_start; exit 0 ;;
      --) shift; break ;;
      -*) usage_start; die "unknown flag: $1" ;;
      *)
        if [ -z "$NAME" ]; then
          NAME="$1"
        else
          usage_start; die "unexpected extra argument: $1"
        fi
        shift
        ;;
    esac
  done

  if [ -z "${NAME:-}" ]; then
    usage_start
    exit 2
  fi

  local SLUG
  SLUG="$(slugify "$NAME")"

  local IMAGE="${CLAUDE_BOTTLE_IMAGE:-claude-bottle:latest}"
  # Default container name is claude-bottle-<slug>. If the user pinned a
  # specific name via CLAUDE_BOTTLE_CONTAINER we honor it as-is below.
  # Otherwise we auto-suffix on conflict so concurrent starts of the
  # same agent get distinct containers (claude-bottle-journal,
  # claude-bottle-journal-2, ...). Final resolution happens just below,
  # after require_docker, since container_exists needs docker reachable.
  local DEFAULT_CONTAINER="claude-bottle-${SLUG}"
  local PINNED_CONTAINER="${CLAUDE_BOTTLE_CONTAINER:-}"

  # When --cwd is on, runtime image is a thin derived image FROM $IMAGE
  # with the user's cwd COPY'd in. Tag it per-agent so the layer cache
  # stays effective across repeated launches of the same agent.
  local RUNTIME_IMAGE="$IMAGE"
  local DERIVED_IMAGE=""
  if [ "$COPY_CWD" = "1" ]; then
    DERIVED_IMAGE="${CLAUDE_BOTTLE_DERIVED_IMAGE:-claude-bottle:cwd-${SLUG}}"
    RUNTIME_IMAGE="$DERIVED_IMAGE"
  fi

  require_docker
  require_jq

  # Resolve the manifest (merge USER_CWD and HOME configs) into a temp file
  # early so it is available for all subsequent manifest calls.
  # Not declared local: the EXIT trap fires after cmd_start returns, so local
  # variables would already be out of scope when cleanup_all runs.
  MANIFEST_FILE="$(mktemp -t claude-bottle-manifest.XXXXXX.json)"
  trap 'rm -f "${MANIFEST_FILE:-}"' EXIT
  manifest_resolve "$USER_CWD" > "$MANIFEST_FILE"

  manifest_require_agent "$MANIFEST_FILE" "$NAME"

  # Not declared local: needed by cleanup_all after cmd_start returns (see MANIFEST_FILE note above).
  CONTAINER=""
  local _suffix=2
  if [ -n "$PINNED_CONTAINER" ]; then
    CONTAINER="$PINNED_CONTAINER"
    if container_exists "$CONTAINER"; then
      die "container '${CONTAINER}' already exists (pinned via CLAUDE_BOTTLE_CONTAINER). Remove it with 'docker rm -f ${CONTAINER}' or unset the override."
    fi
  else
    CONTAINER="$DEFAULT_CONTAINER"
    while container_exists "$CONTAINER"; do
      CONTAINER="${DEFAULT_CONTAINER}-${_suffix}"
      _suffix=$((_suffix + 1))
      if [ "$_suffix" -gt 100 ]; then
        die "could not find a free container name after ${DEFAULT_CONTAINER}-99; clean up old containers with 'docker rm -f <name>'"
      fi
    done
  fi

  # --- Plan resolution (host-only, no container yet) ---

  # Collect the env names (for display) and the skill names (for both
  # display and validation).
  local ENV_NAMES_LIST=""
  local _en
  while IFS= read -r _en; do
    [ -z "$_en" ] && continue
    if [ -z "$ENV_NAMES_LIST" ]; then
      ENV_NAMES_LIST="$_en"
    else
      ENV_NAMES_LIST="${ENV_NAMES_LIST}, ${_en}"
    fi
  done < <(manifest_env_names "$MANIFEST_FILE" "$NAME")

  # CLAUDE_BOTTLE_OAUTH_TOKEN → CLAUDE_CODE_OAUTH_TOKEN forwarding.
  # When the host has the token set, it is always forwarded regardless of the
  # manifest so that every container can authenticate without wiring the token
  # into each agent definition.
  local FORWARD_OAUTH_TOKEN=0
  if [ -n "${CLAUDE_BOTTLE_OAUTH_TOKEN:-}" ]; then
    FORWARD_OAUTH_TOKEN=1
    if [ -z "$ENV_NAMES_LIST" ]; then
      ENV_NAMES_LIST="CLAUDE_CODE_OAUTH_TOKEN"
    else
      ENV_NAMES_LIST="${ENV_NAMES_LIST}, CLAUDE_CODE_OAUTH_TOKEN"
    fi
  fi

  # Skills as an array.
  local SKILL_NAMES=()
  local _sk
  while IFS= read -r _sk; do
    [ -z "$_sk" ] && continue
    SKILL_NAMES+=("$_sk")
  done < <(manifest_skills "$MANIFEST_FILE" "$NAME")

  # Validate every requested skill exists on the host BEFORE the y/N.
  if [ "${#SKILL_NAMES[@]}" -gt 0 ]; then
    skills_validate_all "${SKILL_NAMES[@]}"
  fi

  # Resolve the box referenced by this agent and validate it exists.
  # A box is required — agents without one are rejected before launch.
  local BOX_NAME
  BOX_NAME="$(manifest_agent_box "$MANIFEST_FILE" "$NAME")"
  if [ -z "$BOX_NAME" ]; then
    die "agent '${NAME}' has no 'box' field. Add a box association to this agent in claude-bottle.json."
  fi
  manifest_require_box "$MANIFEST_FILE" "$BOX_NAME"

  # SSH entries come from the agent's box (empty if no box set).
  local SSH_ENTRIES=()
  local _se
  while IFS= read -r _se; do
    [ -z "$_se" ] && continue
    SSH_ENTRIES+=("$_se")
  done < <(manifest_ssh "$MANIFEST_FILE" "$NAME")

  # Validate key files exist on the host BEFORE the y/N.
  if [ "${#SSH_ENTRIES[@]}" -gt 0 ]; then
    ssh_validate_entries "${SSH_ENTRIES[@]}"
  fi

  # Stage env-file + args-file under a mktemp dir; clean up on exit.
  # Not declared local: needed by cleanup_stage after cmd_start returns (see MANIFEST_FILE note above).
  STAGE_DIR="$(mktemp -d -t claude-bottle-stage.XXXXXX)"
  local ENV_FILE="${STAGE_DIR}/agent.env"
  local ARGS_FILE="${STAGE_DIR}/docker-args"
  local PROMPT_FILE="${STAGE_DIR}/prompt.txt"
  : > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  : > "$ARGS_FILE"
  : > "$PROMPT_FILE"
  chmod 600 "$PROMPT_FILE"

  cleanup_stage() {
    if [ -n "${STAGE_DIR:-}" ] && [ -d "$STAGE_DIR" ]; then
      rm -rf "$STAGE_DIR"
    fi
    rm -f "${MANIFEST_FILE:-}"
  }
  trap cleanup_stage EXIT

  # Resolve env entries: prompts secrets (silent /dev/tty), copies
  # interpolated host vars into this process, writes literal pairs to
  # ENV_FILE.
  env_resolve "$MANIFEST_FILE" "$NAME" "$ENV_FILE" "$ARGS_FILE"

  # Read the prompt and write it to PROMPT_FILE. Inside the container the
  # prompt will be passed via `--append-system-prompt-file <path>`, so
  # the content does NOT land on `docker exec` argv even if it grows
  # arbitrarily large.
  local PROMPT_CONTENT
  PROMPT_CONTENT="$(manifest_prompt "$MANIFEST_FILE" "$NAME")"
  printf '%s' "$PROMPT_CONTENT" > "$PROMPT_FILE"

  local PROMPT_LEN="${#PROMPT_CONTENT}"
  local PROMPT_FIRST_LINE
  PROMPT_FIRST_LINE="$(printf '%s' "$PROMPT_CONTENT" | awk 'NR==1{print; exit}')"

  # --- Show plan + confirm ---

  printf '\n' >&2
  info "agent           : ${NAME}"
  info "image           : ${IMAGE}"
  if [ -n "$DERIVED_IMAGE" ]; then
    info "cwd             : ${USER_CWD} -> /home/node/workspace (derived: ${DERIVED_IMAGE})"
  fi
  info "container       : ${CONTAINER}"
  info "stage dir       : ${STAGE_DIR}"
  if [ -n "$ENV_NAMES_LIST" ]; then
    info "env (names only): ${ENV_NAMES_LIST}"
  else
    info "env (names only): (none)"
  fi
  if [ "${#SKILL_NAMES[@]}" -gt 0 ]; then
    info "skills          : ${SKILL_NAMES[*]}"
  else
    info "skills          : (none)"
  fi
  if [ -n "$BOX_NAME" ]; then
    info "box             : ${BOX_NAME}"
    if [ "${#SSH_ENTRIES[@]}" -gt 0 ]; then
      local _ssh_names="" _se
      for _se in "${SSH_ENTRIES[@]}"; do
        local _n
        _n="$(printf '%s' "$_se" | jq -r '.Host')"
        _ssh_names="${_ssh_names:+${_ssh_names}, }${_n}"
      done
      info "  ssh hosts     : ${_ssh_names}"
    else
      info "  ssh hosts     : (none)"
    fi
  else
    info "box             : (none)"
  fi
  info "prompt          : ${PROMPT_LEN} chars; first line: ${PROMPT_FIRST_LINE:-(empty)}"
  printf '\n' >&2

  if [ "$DRY_RUN" = "1" ]; then
    info "dry-run requested; not starting container."
    exit 0
  fi

  printf 'claude-bottle: launch this agent? [y/N] ' >&2
  local REPLY
  IFS= read -r REPLY </dev/tty
  case "$REPLY" in
    y|Y|yes|YES) ;;
    *) info "aborted by user"; exit 0 ;;
  esac

  # --- Build & launch ---

  build_image "$IMAGE" "$REPO_DIR"
  if [ -n "$DERIVED_IMAGE" ]; then
    build_image_with_cwd "$DERIVED_IMAGE" "$IMAGE" "$USER_CWD"
  fi

  # Cleanup container on exit too. Compose with stage cleanup.
  cleanup_all() {
    if container_exists "$CONTAINER"; then
      docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi
    cleanup_stage
  }
  # Replaces the cleanup_stage EXIT trap above; cleanup_all calls cleanup_stage internally.
  trap cleanup_all EXIT INT TERM

  # Assemble docker run argv:
  #   - --rm -d --name CONTAINER
  #   - --env-file ENV_FILE  (only if it has any entries)
  #   - one `-e NAME` pair per line in ARGS_FILE  (secret + interpolated)
  #   - IMAGE
  #   - sleep infinity (so we can `docker exec` an interactive session)
  local DOCKER_ARGS=(--rm -d --name "$CONTAINER")
  if [ -s "$ENV_FILE" ]; then
    DOCKER_ARGS+=(--env-file "$ENV_FILE")
  fi
  # Read pairs of (-e, NAME) lines from ARGS_FILE.
  local flag vname
  while IFS= read -r flag; do
    [ -z "$flag" ] && continue
    IFS= read -r vname || break
    DOCKER_ARGS+=("$flag" "$vname")
  done <"$ARGS_FILE"
  if [ "$FORWARD_OAUTH_TOKEN" = "1" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_BOTTLE_OAUTH_TOKEN"
    DOCKER_ARGS+=(-e CLAUDE_CODE_OAUTH_TOKEN)
  fi
  DOCKER_ARGS+=("$RUNTIME_IMAGE" sleep infinity)

  info "starting container ${CONTAINER} from ${RUNTIME_IMAGE}"
  # The pre-check loop above is best-effort: two parallel starts can both
  # observe the same bare name as free, so we also retry here when docker
  # rejects the run with a name conflict. Pinned names skip the retry —
  # user-chosen, user-owned.
  local RUN_ERR_FILE="${STAGE_DIR}/docker-run.err"
  local RUN_ERR_TEXT
  while :; do
    : > "$RUN_ERR_FILE"
    if docker run "${DOCKER_ARGS[@]}" >/dev/null 2>"$RUN_ERR_FILE"; then
      break
    fi
    RUN_ERR_TEXT="$(cat "$RUN_ERR_FILE")"
    if [ -n "$PINNED_CONTAINER" ] || ! printf '%s' "$RUN_ERR_TEXT" | grep -q "is already in use"; then
      printf '%s\n' "$RUN_ERR_TEXT" >&2
      die "docker run failed for container '${CONTAINER}'"
    fi
    if [ "$_suffix" -gt 100 ]; then
      die "could not find a free container name after ${DEFAULT_CONTAINER}-99 retries; clean up old containers with 'docker rm -f <name>'"
    fi
    CONTAINER="${DEFAULT_CONTAINER}-${_suffix}"
    _suffix=$((_suffix + 1))
    DOCKER_ARGS[3]="$CONTAINER"
    info "name conflict; retrying as ${CONTAINER}"
  done

  # Copy prompt file into the container WITHOUT putting its contents on
  # argv. `docker cp` reads the file from disk and streams it in.
  local CONTAINER_PROMPT_PATH="${CLAUDE_BOTTLE_CONTAINER_HOME:-/home/node}/.claude-bottle-prompt.txt"
  docker cp "$PROMPT_FILE" "${CONTAINER}:${CONTAINER_PROMPT_PATH}" >/dev/null
  # `docker cp` preserves the host file's numeric UID, which on hosts where
  # the user is not uid 1000 (e.g. macOS uid 501) leaves the in-container
  # file unreadable by the `node` user. Re-own and re-mode as root inside
  # the container so `node` can read its own mode-600 prompt regardless of
  # host UID.
  docker exec -u 0 "$CONTAINER" chown node:node "$CONTAINER_PROMPT_PATH" >/dev/null
  docker exec -u 0 "$CONTAINER" chmod 600 "$CONTAINER_PROMPT_PATH" >/dev/null

  # Copy each requested skill.
  if [ "${#SKILL_NAMES[@]}" -gt 0 ]; then
    skills_copy_into "$CONTAINER" "${SKILL_NAMES[@]}"
  fi

  # Set up SSH keys and config.
  if [ "${#SSH_ENTRIES[@]}" -gt 0 ]; then
    ssh_setup "$CONTAINER" "$STAGE_DIR" "${SSH_ENTRIES[@]}"
  fi

  # When --cwd is on, ship the host repo's .git directory in via `docker cp`
  # rather than the build-time COPY. Two reasons: (1) build-time COPY honors
  # the host project's .dockerignore, which often excludes .git/ (e.g. the
  # openemr fork) — without .git the agent inside has no branch, no remotes,
  # and can't commit; (2) keeping .git out of the cached image layer avoids
  # bloating the layer (a real repo's .git can be several GB) and avoids
  # baking a stale snapshot of refs/index into the image. The cp at run
  # time means the agent always sees the host's current refs.
  if [ "$COPY_CWD" = "1" ] && [ -d "$USER_CWD/.git" ]; then
    info "copying ${USER_CWD}/.git -> ${CONTAINER}:/home/node/workspace/.git"
    docker cp "$USER_CWD/.git" "${CONTAINER}:/home/node/workspace/.git" >/dev/null
    docker exec -u 0 "$CONTAINER" chown -R node:node /home/node/workspace/.git >/dev/null
  fi

  info "attaching interactive claude session (Ctrl-D or 'exit' to leave; container will be removed)"
  # --remote-control: enable Remote Control (hidden flag; see --remote-control-session-name-prefix
  #   in `claude --help` — the prefix flag is the only surfaced piece, the toggle itself is hidden,
  #   same pattern as --append-system-prompt-file).
  # --dangerously-skip-permissions: bypass permission prompts. Safe here because the whole point of
  #   claude-bottle is sandboxing claude inside a container (see CLAUDE.md "What this is").
  local CLAUDE_ARGS=(--remote-control --dangerously-skip-permissions)
  # `|| true` so a non-zero exit from the REPL doesn't skip the trap output.
  if [ -n "$PROMPT_CONTENT" ]; then
    docker exec -it "$CONTAINER" claude "${CLAUDE_ARGS[@]}" --append-system-prompt-file "$CONTAINER_PROMPT_PATH" || true
  else
    docker exec -it "$CONTAINER" claude "${CLAUDE_ARGS[@]}" || true
  fi

  info "session ended; container ${CONTAINER} will be removed"
}

# ---------------------------------------------------------------------------
# cmd_init — interactively populate a new agent and write it to either
# ~/claude-bottle.json (user) or ./claude-bottle.json (project).
#
# Prompts for:
#   - agent name (required)
#   - env vars: name + mode (secret / interpolated / literal)
#   - skills (space-separated)
#   - system prompt (multi-line, terminated by a lone ".")
#   - SSH host entries (optional)
#
# Merges the new agent into the target file if it already exists
# (existing agents are preserved; name conflicts prompt for confirmation).
# ---------------------------------------------------------------------------
cmd_init() {
  usage_init() {
    printf 'usage: %s init <user|project>\n' "$(basename "$0")" >&2
    printf '  user      add the agent to ~/claude-bottle.json\n' >&2
    printf '  project   add the agent to ./claude-bottle.json in the current directory\n' >&2
  }

  if [ "$#" -lt 1 ]; then
    usage_init
    exit 2
  fi

  local SCOPE TARGET_FILE
  case "$1" in
    -h|--help) usage_init; exit 0 ;;
    user)    SCOPE="user";    TARGET_FILE="${HOME}/claude-bottle.json" ;;
    project) SCOPE="project"; TARGET_FILE="${USER_CWD}/claude-bottle.json" ;;
    *) usage_init; die "expected 'user' or 'project', got: $1" ;;
  esac

  require_jq

  printf '\n' >&2
  info "claude-bottle init — adding a new agent to ${TARGET_FILE}"
  printf '\n' >&2

  # --- Agent name ---
  local AGENT_NAME=""
  while [ -z "$AGENT_NAME" ]; do
    printf 'Agent name: ' >&2
    IFS= read -r AGENT_NAME </dev/tty
    # Normalise spaces to hyphens so the name is slug-friendly.
    AGENT_NAME="$(printf '%s' "$AGENT_NAME" | tr ' ' '-')"
    if [ -z "$AGENT_NAME" ]; then
      warn "agent name cannot be empty"
    fi
  done

  # Warn (but do not block) if the name is not already a clean slug.
  if ! printf '%s' "$AGENT_NAME" | grep -qE '^[a-z0-9][a-z0-9-]*$'; then
    warn "agent name '${AGENT_NAME}' contains non-slug characters; it will still work but may cause container naming issues"
  fi

  # Check for an existing agent with the same name.
  if [ -f "$TARGET_FILE" ] && jq -e --arg n "$AGENT_NAME" '.agents | has($n)' "$TARGET_FILE" >/dev/null 2>&1; then
    printf 'claude-bottle: agent "%s" already exists in %s. Overwrite? [y/N] ' "$AGENT_NAME" "$TARGET_FILE" >&2
    local _ow
    IFS= read -r _ow </dev/tty
    case "$_ow" in
      y|Y|yes|YES) ;;
      *) info "aborted"; return 0 ;;
    esac
  fi

  # --- Skills ---
  printf '\n' >&2
  printf 'Skills (space or comma separated, or Enter for none): ' >&2
  local _skills_input=""
  IFS= read -r _skills_input </dev/tty

  local SKILLS_JSON='[]'
  if [ -n "$_skills_input" ]; then
    local _skill_arr=()
    local _cleaned
    _cleaned="$(printf '%s' "$_skills_input" | tr ',' ' ')"
    read -ra _skill_arr <<< "$_cleaned"
    if [ "${#_skill_arr[@]}" -gt 0 ]; then
      SKILLS_JSON="$(printf '%s\n' "${_skill_arr[@]}" | jq -R . | jq -s .)"
    fi
  fi

  # --- System prompt ---
  printf '\n' >&2
  info "System prompt — enter text, then a lone '.' on its own line to finish (just '.' to leave empty):"
  local PROMPT_CONTENT="" _pline _pfirst=1
  while :; do
    IFS= read -r _pline </dev/tty
    [ "$_pline" = "." ] && break
    if [ "$_pfirst" = "1" ]; then
      PROMPT_CONTENT="$_pline"
      _pfirst=0
    else
      PROMPT_CONTENT="${PROMPT_CONTENT}"$'\n'"${_pline}"
    fi
  done

  # --- Box association ---
  printf '\n' >&2
  printf 'Associate this agent with a box? [y/N] ' >&2
  local _box_yn=""
  IFS= read -r _box_yn </dev/tty
  local BOX_NAME=""
  local BOX_SSH_JSON='[]'
  local BOX_ENV_JSON='{}'
  local _box_exists=0
  case "$_box_yn" in
    y|Y|yes|YES)
      while [ -z "$BOX_NAME" ]; do
        printf '  Box name: ' >&2
        IFS= read -r BOX_NAME </dev/tty
        BOX_NAME="$(printf '%s' "$BOX_NAME" | tr ' ' '-')"
        if [ -z "$BOX_NAME" ]; then
          warn "box name cannot be empty"
        fi
      done

      # Check whether the box already exists in the target file.
      if [ -f "$TARGET_FILE" ] && jq -e --arg b "$BOX_NAME" '.boxes | has($b)' "$TARGET_FILE" >/dev/null 2>&1; then
        _box_exists=1
        info "Box '${BOX_NAME}' already exists in ${TARGET_FILE}; agent will reference it."
      else
        info "Creating new box '${BOX_NAME}'."

        # --- Env vars (stored on the box) ---
        printf '\n' >&2
        info "Env vars — enter each var name then its mode. Press Enter with no name to finish."
        info "  Modes:  secret (prompt at runtime) | interpolated (read from host env) | literal (hardcoded value)"
        while :; do
          printf '\n  Var name (or Enter to finish): ' >&2
          local _vname=""
          IFS= read -r _vname </dev/tty
          [ -z "$_vname" ] && break

          printf '  Mode [secret/interpolated/literal] (default: secret): ' >&2
          local _vmode=""
          IFS= read -r _vmode </dev/tty
          _vmode="${_vmode:-secret}"

          local _vval=""
          case "$_vmode" in
            secret)
              printf '  Prompt message shown to user (default: "enter %s"): ' "$_vname" >&2
              local _smsg=""
              IFS= read -r _smsg </dev/tty
              if [ -n "$_smsg" ]; then
                _vval="?${_smsg}"
              else
                _vval="?"
              fi
              ;;
            interpolated)
              printf '  Host env var to read from (default: %s): ' "$_vname" >&2
              local _hvar=""
              IFS= read -r _hvar </dev/tty
              _hvar="${_hvar:-${_vname}}"
              _vval="\${${_hvar}}"
              ;;
            literal)
              printf '  Value: ' >&2
              IFS= read -r _vval </dev/tty
              ;;
            *)
              warn "unknown mode '${_vmode}'; using secret"
              _vval="?"
              ;;
          esac

          BOX_ENV_JSON="$(printf '%s' "$BOX_ENV_JSON" | jq --arg k "$_vname" --arg v "$_vval" '.[$k] = $v')"
        done

        printf '  Add SSH host entries to this box? [y/N] ' >&2
        local _ssh_yn=""
        IFS= read -r _ssh_yn </dev/tty
        case "$_ssh_yn" in
          y|Y|yes|YES)
            while :; do
              printf '\n    SSH Host alias (or Enter to finish): ' >&2
              local _shost=""
              IFS= read -r _shost </dev/tty
              [ -z "$_shost" ] && break

              printf '    Hostname (actual hostname or IP): ' >&2
              local _shostname=""
              IFS= read -r _shostname </dev/tty

              printf '    User: ' >&2
              local _suser=""
              IFS= read -r _suser </dev/tty

              printf '    Port (default: 22): ' >&2
              local _sport=""
              IFS= read -r _sport </dev/tty
              _sport="${_sport:-22}"
              if ! printf '%s' "$_sport" | grep -qE '^[0-9]+$'; then
                warn "port must be a number; defaulting to 22"
                _sport="22"
              fi

              printf '    IdentityFile (path to private key on host): ' >&2
              local _sidentity=""
              IFS= read -r _sidentity </dev/tty

              printf '    KnownHostKey (optional, Enter to skip): ' >&2
              local _skhk=""
              IFS= read -r _skhk </dev/tty

              local _entry
              _entry="$(jq -n \
                --arg    host     "$_shost" \
                --arg    hostname "$_shostname" \
                --arg    user     "$_suser" \
                --argjson port    "$_sport" \
                --arg    identity "$_sidentity" \
                '{Host: $host, Hostname: $hostname, User: $user, Port: $port, IdentityFile: $identity}')"
              if [ -n "$_skhk" ]; then
                _entry="$(printf '%s' "$_entry" | jq --arg khk "$_skhk" '. + {KnownHostKey: $khk}')"
              fi
              BOX_SSH_JSON="$(printf '%s' "$BOX_SSH_JSON" | jq --argjson e "$_entry" '. + [$e]')"
            done
            ;;
        esac
      fi
      ;;
  esac

  # --- Build agent JSON ---
  local AGENT_JSON
  AGENT_JSON="$(jq -n \
    --argjson skills "$SKILLS_JSON" \
    --arg     prompt "$PROMPT_CONTENT" \
    '{skills: $skills, prompt: $prompt}')"

  if [ -n "$BOX_NAME" ]; then
    AGENT_JSON="$(printf '%s' "$AGENT_JSON" | jq --arg b "$BOX_NAME" '. + {box: $b}')"
  fi

  # Build the new entry. When creating a brand-new box, include it so the
  # merge below writes both boxes and agents in one step. env lives on the box.
  local NEW_ENTRY
  if [ -n "$BOX_NAME" ] && [ "$_box_exists" = "0" ]; then
    NEW_ENTRY="$(jq -n \
      --arg    box_name  "$BOX_NAME" \
      --argjson box_env   "$BOX_ENV_JSON" \
      --argjson box_ssh   "$BOX_SSH_JSON" \
      --arg    agent_name "$AGENT_NAME" \
      --argjson agent      "$AGENT_JSON" \
      '{"boxes": {($box_name): {env: $box_env, ssh: $box_ssh}}, "agents": {($agent_name): $agent}}')"
  else
    NEW_ENTRY="$(jq -n \
      --arg    name  "$AGENT_NAME" \
      --argjson agent "$AGENT_JSON" \
      '{"agents": {($name): $agent}}')"
  fi

  # --- Write / merge into target file ---
  printf '\n' >&2

  local TMP_FILE
  TMP_FILE="$(mktemp -t claude-bottle-init.XXXXXX.json)"

  if [ -f "$TARGET_FILE" ]; then
    if ! jq -e . "$TARGET_FILE" >/dev/null 2>&1; then
      rm -f "$TMP_FILE"
      die "${TARGET_FILE} exists but is not valid JSON; fix or remove it first"
    fi
    if ! printf '%s' "$NEW_ENTRY" | jq -s '{
      "boxes":  ((.[0].boxes  // {}) * (.[1].boxes  // {})),
      "agents": ((.[0].agents // {}) * (.[1].agents // {}))
    }' "$TARGET_FILE" - > "$TMP_FILE"; then
      rm -f "$TMP_FILE"
      die "failed to merge agent into ${TARGET_FILE}"
    fi
  else
    if ! printf '%s\n' "$NEW_ENTRY" > "$TMP_FILE"; then
      rm -f "$TMP_FILE"
      die "failed to write ${TARGET_FILE}"
    fi
  fi

  mv "$TMP_FILE" "$TARGET_FILE"

  info "Agent '${AGENT_NAME}' written to ${TARGET_FILE}."
  info "Run '$(basename "$0") info ${AGENT_NAME}' to verify."
  printf '\n' >&2
}

# ---------------------------------------------------------------------------
# cmd_edit — open an agent entry in vim at the line where its key appears.
# ---------------------------------------------------------------------------
cmd_edit() {
  usage_edit() {
    printf 'usage: %s edit <user|project> <name>\n' "$(basename "$0")" >&2
    printf '  user      edit an agent in ~/claude-bottle.json\n' >&2
    printf '  project   edit an agent in ./claude-bottle.json in the current directory\n' >&2
    printf '  <name>    name of the agent to jump to\n' >&2
  }

  if [ "$#" -lt 2 ]; then
    usage_edit
    exit 2
  fi

  local TARGET_FILE
  case "$1" in
    -h|--help) usage_edit; exit 0 ;;
    user)    TARGET_FILE="${HOME}/claude-bottle.json" ;;
    project) TARGET_FILE="${USER_CWD}/claude-bottle.json" ;;
    *) usage_edit; die "expected 'user' or 'project', got: $1" ;;
  esac

  local NAME="$2"

  require_jq

  if [ ! -f "$TARGET_FILE" ]; then
    die "${TARGET_FILE} does not exist"
  fi

  if ! jq -e --arg n "$NAME" '.agents | has($n)' "$TARGET_FILE" >/dev/null 2>&1; then
    die "agent '${NAME}' not found in ${TARGET_FILE}"
  fi

  local LINE
  LINE="$(grep -Fn "\"${NAME}\"" "$TARGET_FILE" | head -1 | cut -d: -f1)"
  LINE="${LINE:-1}"

  exec vim +"${LINE}" "$TARGET_FILE"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

if [ "$#" -lt 1 ]; then
  usage
  exit 2
fi

COMMAND="$1"
shift

case "$COMMAND" in
  build)   cmd_build ;;
  cleanup) cmd_cleanup ;;
  edit)    cmd_edit "$@" ;;
  info)    cmd_info "$@" ;;
  init)    cmd_init "$@" ;;
  list)    cmd_list "$@" ;;
  start)   cmd_start "$@" ;;
  -h|--help) usage; exit 0 ;;
  *) usage; die "unknown command: ${COMMAND}" ;;
esac
