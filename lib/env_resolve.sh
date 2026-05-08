#!/usr/bin/env bash
# Env resolver. Walks the env entries for one agent in claude-bottle.json
# and produces:
#   1. The list of `docker run` arg fragments needed to forward each var.
#      Both `secret` and `interpolated` entries become `-e NAME` (no
#      `=value`) so Docker inherits the value from this process env
#      without rendering it on argv or persisting it to disk.
#      Only `literal` entries are written to a host-disk env-file and
#      forwarded with `--env-file <path>`.
#   2. The export side-effect of populating this process's env with
#      secret values prompted from the user, and with interpolated
#      values copied from the matching host var, so `-e NAME` actually
#      has something to inherit.
#
# Each env entry is a JSON string. Mode is selected by sentinel prefix:
#   "?"           → secret (prompt at runtime). Bare "?" uses a default
#                   prompt; "?<message>" uses <message> as the prompt body.
#   "${HOST_VAR}" → interpolated from $HOST_VAR in the host process env
#   any other str → literal (the JSON string is the value verbatim)
# A literal whose text starts with "?" or matches "${IDENT}" is not
# representable in v1 — pick a different value or change the convention.
#
# Critical rules (re-read CLAUDE.md "Checking env vars safely"):
#   - NEVER echo, log, or interpolate the value of a secret or
#     interpolated env var. Both modes are treated as potentially
#     sensitive: nothing about their value (other than presence /
#     length) ever lands on disk, in a log line, or on argv.
#   - The env-file written for literal values lives under `mktemp -d`
#     with mode 600 and is removed on script exit by the caller's trap.
#     Secrets and interpolated values never go to this file.
#   - Errors mention only the variable NAME, never any portion of the value.
#
# Idempotent: safe to source multiple times.

if [ -n "${CLAUDE_BOTTLE_LIB_ENV_RESOLVE_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_LIB_ENV_RESOLVE_SOURCED=1

_iso_lib_env_resolve_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./log.sh
. "${_iso_lib_env_resolve_dir}/log.sh"
# shellcheck source=./manifest.sh
. "${_iso_lib_env_resolve_dir}/manifest.sh"

# env_entry_kind <raw-string> — prints "secret", "interpolated", or
# "literal" based on the sentinel form of the entry. Never echoes the
# value of an interpolated entry — only its host-var NAME via the
# captured submatch. Secret-mode prompt text (everything after the
# leading "?") is extracted by env_entry_secret_prompt, not here.
env_entry_kind() {
  local raw="${1-}"
  case "$raw" in
    \?*)
      printf 'secret'
      return 0
      ;;
  esac
  if [[ "$raw" =~ ^\$\{[A-Za-z_][A-Za-z0-9_]*\}$ ]]; then
    printf 'interpolated'
    return 0
  fi
  printf 'literal'
}

# env_entry_secret_prompt <raw-string> — for a secret entry (one whose
# raw value starts with "?"), prints the prompt body (everything after
# the leading "?"). Empty for a bare "?", which signals "use default
# prompt." Caller is responsible for falling back to a default.
env_entry_secret_prompt() {
  local raw="${1-}"
  printf '%s' "${raw#\?}"
}

# env_entry_interpolated_from <raw-string> — for an interpolated entry,
# prints the host var name (the identifier between `${` and `}`).
env_entry_interpolated_from() {
  local raw="${1-}"
  local inner="${raw#\$\{}"
  inner="${inner%\}}"
  printf '%s' "$inner"
}

# _read_secret_silent <NAME> [<prompt-body>] — prompt the user for a
# secret value on the tty without echoing the keystrokes. Stores the
# value in the global variable named by $1 via printf -v. Stdin
# redirection from /dev/tty so this still works under `<(...)` and
# other non-tty stdin situations.
#
# If <prompt-body> is provided and non-empty, the prompt rendered to
# the tty is "<prompt-body> (input hidden): "; otherwise it falls back
# to "claude-bottle: secret value for <NAME> (input hidden): ". The "(input
# hidden): " tail is always appended by this function — manifest
# authors write the message text only.
#
# We never `echo "$VALUE"` or interpolate it elsewhere; the only consumer
# is `export "$NAME=$VALUE"` immediately below.
_read_secret_silent() {
  local target="${1:?_read_secret_silent: missing target var name}"
  local prompt_body="${2-}"
  local value=""
  # Use the controlling tty for both the prompt and the read so this is
  # robust even if stdin is a pipe.
  if [ ! -t 0 ] && [ ! -t 2 ]; then
    die "cannot prompt for secret '${target}': no tty available. Run from an interactive shell."
  fi
  # `printf` to /dev/tty for the prompt, `read -s` from /dev/tty for the value.
  if [ -n "$prompt_body" ]; then
    printf '%s (input hidden): ' "$prompt_body" >/dev/tty
  else
    printf 'claude-bottle: secret value for %s (input hidden): ' "$target" >/dev/tty
  fi
  # IFS= read -rs to read one line, raw, silent.
  IFS= read -rs value </dev/tty
  printf '\n' >/dev/tty
  if [ -z "$value" ]; then
    die "empty value provided for secret '${target}'. Re-run and supply a value."
  fi
  # Indirect assignment — never expose value via expansion in a string we
  # log or pass anywhere else.
  printf -v "$target" '%s' "$value"
  # Scrub our local copy.
  value=""
}

# env_resolve <manifest_file> <agent_name> <env_file_path> <out_args_path>
#
# Iterates the agent's env entries. For each entry:
#   - secret      → ALWAYS prompt for the value (even if already set in
#                   this process env), export it into this process, and
#                   append `-e NAME` to <out_args_path> (one arg per
#                   line; a NAME with no `=value`).
#   - interpolated→ read the host process env value of the named host var;
#                   if unset, die with the host-var name. Copy into this
#                   process under the target name and append `-e NAME` to
#                   <out_args_path>. Never written to disk.
#   - literal     → append `NAME=VALUE` to <env_file_path>; the resolver
#                   does NOT add anything to <out_args_path> for this entry
#                   (the caller adds a single `--env-file <env_file_path>`
#                   if the file is non-empty).
#
# The caller is responsible for:
#   - creating <env_file_path> as an empty file with mode 600 under a
#     mktemp dir,
#   - creating <out_args_path> as an empty file,
#   - cleaning both up on exit (trap),
#   - reading <out_args_path> line-by-line into the docker-run argv.
#
# Returns 0 on success, dies on any error.
env_resolve() {
  local manifest_file="${1:?env_resolve: missing manifest file}"
  local agent="${2:?env_resolve: missing agent name}"
  local env_file="${3:?env_resolve: missing env_file path}"
  local out_args="${4:?env_resolve: missing out_args path}"

  local name raw kind from prompt_body
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    raw="$(manifest_env_entry "$manifest_file" "$agent" "$name")"
    kind="$(env_entry_kind "$raw")"
    case "$kind" in
      secret)
        # Always prompt — never trust an already-exported host value.
        # A "?"-prefixed entry in the manifest is the user's signal
        # that this variable must be supplied interactively at launch
        # time, even if a same-named var is already in the parent shell.
        prompt_body="$(env_entry_secret_prompt "$raw")"
        _read_secret_silent "$name" "$prompt_body"
        # Export so child processes (docker run) inherit. `-e NAME` (no
        # value) on docker run picks up from the parent process env.
        export "${name?}"
        printf -- '-e\n%s\n' "$name" >>"$out_args"
        ;;
      interpolated)
        from="$(env_entry_interpolated_from "$raw")"
        # Treat interpolated values as potentially sensitive: never write
        # them to disk and never put them on argv. Instead, copy the host
        # var into THIS process under the target name (so Docker can
        # inherit it via `-e NAME`), and emit `-e NAME` in the args file.
        # The check below uses indirect expansion only to determine
        # presence — no expansion of the value lands in any output.
        if [ -z "${!from-}" ]; then
          die "env entry ${name} is interpolated from \$${from}, but \$${from} is unset or empty in the host environment."
        fi
        # Copy via printf -v + indirect read. We use a brief local then
        # immediately export under $name and scrub the local.
        local _interp_val
        _interp_val="${!from}"
        printf -v "${name?}" '%s' "$_interp_val"
        _interp_val=""
        export "${name?}"
        printf -- '-e\n%s\n' "$name" >>"$out_args"
        ;;
      literal)
        # Multi-line literal values are not supported by docker --env-file,
        # so reject them up front rather than letting docker fail with a
        # confusing message.
        case "$raw" in
          *$'\n'*) die "env entry ${name} (literal) contains a newline; docker --env-file cannot represent multi-line values." ;;
        esac
        printf '%s=%s\n' "$name" "$raw" >>"$env_file"
        ;;
    esac
  done < <(manifest_env_names "$manifest_file" "$agent")
}
