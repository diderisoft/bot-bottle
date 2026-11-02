"""Host-side rendering for git-gate: the agent's `.gitconfig` insteadOf
rewrites, known_hosts lines, and the entrypoint, provisioning, pre-receive,
access-hook and gitleaks files the gateway runs."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..contract.git_trailers import TRAILER_MODES, TRAILER_VALUE_RE
from ..contract.identity import IDENTITY_HEADER
from ..manifest import ManifestBottle, ManifestGitEntry
from ..gateway.egress.dlp_detectors import TOKEN_PATTERNS



@dataclass(frozen=True)
class GitGateUpstream:
    """One bare repo on the gate. `name` sets `/git/<name>.git`, the rewritten
    agent URL and the credential paths (`/git-gate/creds/<name>-key`,
    `-known_hosts`). `identity_file` is the host key path; `known_host_key` is
    the manifest's host key."""

    name: str
    upstream_url: str
    upstream_host: str
    upstream_port: str
    identity_file: str
    known_host_key: str
    known_hosts_file: Path = Path()
    # False for a read-only grant: the gate refuses pushes to it.
    writable: bool = True
    # "https" (a GitHub repo over HTTPS with the user's token, ADR 0014) or
    # "ssh" (a deploy key or static key). For https, `token_secret` names the
    # host secret the token comes from; the gate gets it in memory only.
    transport: str = "ssh"
    token_secret: str = ""


def _upstream(e: ManifestGitEntry) -> GitGateUpstream:
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.git import git_transport
    if git_transport(e.Key) == "https":
        return GitGateUpstream(
            name=e.Name, upstream_url=f"https://{e.UpstreamHost}/{e.UpstreamPath}",
            upstream_host=e.UpstreamHost, upstream_port="443", identity_file="",
            known_host_key="", writable=not e.Key.read_only, transport="https",
            token_secret=e.Key.forge_token_env)
    return GitGateUpstream(
        name=e.Name, upstream_url=e.Upstream, upstream_host=e.UpstreamHost,
        upstream_port=e.UpstreamPort, identity_file=e.IdentityFile,
        known_host_key=e.KnownHostKey, writable=not e.Key.read_only)


def git_gate_upstreams_for_bottle(bottle: ManifestBottle) -> tuple[GitGateUpstream, ...]:
    """Each `bottle.git` entry as a GitGateUpstream (names were checked at
    load)."""
    return tuple(_upstream(e) for e in bottle.git)


def _gitconfig_validate_value(field: str, value: str) -> None:
    """ValueError if `value` would break gitconfig syntax."""
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"git-gate: {field} contains a newline, which would inject "
            f"arbitrary gitconfig keys; rejecting manifest entry"
        )


def git_gate_render_gitconfig(
    entries: tuple[ManifestGitEntry, ...], gate_host: str, *, identity_token: str = "",
) -> str:
    """The agent's ~/.gitconfig insteadOf rewrites to the gate at
    `http://<gate_host>/`, and the identity header. The header is written even
    without entries, so a repo granted later (PRD 0105) only adds a rewrite.
    Neither gives ""."""
    if not entries and not identity_token:
        return ""
    out = [
        "# bot-bottle git-gate: every git operation against\n",
        "# a declared upstream routes through the gate, which mirrors\n",
        "# the upstream bidirectionally (gitleaks-scanned push;\n",
        "# fetch-from-upstream-before-every-upload-pack via access-hook).\n",
    ]
    # The identity token goes as a header scoped to the gate's URL.
    if identity_token:
        _gitconfig_validate_value("identity_token", identity_token)
        out.append(f'[http "http://{gate_host}/"]\n')
        out.append(f"\textraHeader = {IDENTITY_HEADER}: {identity_token}\n")
    for entry in entries:
        _gitconfig_validate_value(f"repos[{entry.Name!r}].url", entry.Upstream)
        out.append(f'[url "http://{gate_host}/{entry.Name}.git"]\n')
        out.append(f"\tinsteadOf = {entry.Upstream}\n")
        if entry.RemoteKey and entry.RemoteKey != entry.UpstreamHost:
            port = (
                f":{entry.UpstreamPort}"
                if entry.UpstreamPort and entry.UpstreamPort != "22"
                else ""
            )
            alias = (
                f"ssh://{entry.UpstreamUser}@{entry.RemoteKey}{port}/"
                f"{entry.UpstreamPath}"
            )
            _gitconfig_validate_value(f"repos[{entry.Name!r}].url (resolved alias)", alias)
            out.append(f"\tinsteadOf = {alias}\n")
    return "".join(out)


def git_gate_known_hosts_line(host: str, port: str, key: str) -> str:
    """`host[:port] key` for known_hosts, with `[host]:port` for non-22 ports."""
    if port and port != "22":
        target = f"[{host}]:{port}"
    else:
        target = host
    return f"{target} {key}\n"


def _git_gate_init_repo_fn(repo_root: str, creds_dir: str) -> list[str]:
    """The `init_repo` shell function for a repo root and creds dir."""
    return [
        "init_repo() {",
        "  name=$1",
        "  upstream_url=$2",
        "  read_only=$3",
        "  transport=${4:-ssh}",
        "  token_file=${5:-}",
        f"  keyfile={creds_dir}/${{name}}-key",
        f"  hostsfile={creds_dir}/${{name}}-known_hosts",
        "",
        # Best effort: mounted files may be read-only and already 0600.
        "  chmod 600 \"$keyfile\" 2>/dev/null || true",
        "  if [ -f \"$hostsfile\" ]; then",
        "    chmod 600 \"$hostsfile\" 2>/dev/null || true",
        "  fi",
        "",
        f"  repo={repo_root}/${{name}}.git",
        "  if [ ! -d \"$repo\" ]; then",
        "    git init --bare \"$repo\" >/dev/null",
        # `--mirror=fetch` makes `git fetch origin` mirror every upstream ref,
        # while an explicit `git push origin <ref>:<ref>` still pushes one ref.
        "    git -C \"$repo\" remote add --mirror=fetch origin \"$upstream_url\"",
        "  fi",
        # A re-provision may change the transport (ssh to https or back).
        "  git -C \"$repo\" remote set-url origin \"$upstream_url\"",
        "  git -C \"$repo\" config git-gate.transport \"$transport\"",
        "  git -C \"$repo\" config git-gate.tokenFile \"$token_file\"",
        "  git -C \"$repo\" config git-gate.identityFile \"$keyfile\"",
        "  git -C \"$repo\" config git-gate.knownHosts \"$hostsfile\"",
        "  git -C \"$repo\" config receive.denyCurrentBranch ignore",
        "  git -C \"$repo\" config receive.advertisePushOptions true",
        "  git -C \"$repo\" config http.receivepack true",
        "  git -C \"$repo\" config git-gate.readOnly \"$read_only\"",
        # The commit-trailer check (PRD 0112): its mode, the value it wants
        # and the commits it exempts.
        "  git -C \"$repo\" config git-gate.trailers \"$trailers_mode\"",
        "  git -C \"$repo\" config git-gate.trailer \"$trailer\"",
        "  git -C \"$repo\" config git-gate.baseline \"$baseline\"",
        "  install -m 755 /etc/git-gate/pre-receive \"$repo/hooks/pre-receive\"",
        "}",
    ]


# Embedded unquoted in the script, so limited to a shell- and path-safe
# alphabet.
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


def git_gate_render_provision(
    bottle_id: str, upstreams: tuple[GitGateUpstream, ...],
    *, trailer_mode: str, trailer: str, baseline: str,
) -> str:
    """POSIX sh that inits one bottle's bare repos under `/git/<bottle_id>/`
    with credentials from `/git-gate/creds/<bottle_id>/`, and sets how
    pre-receive checks the `Bot-Bottle` trailer (`trailer_mode`) for which
    value (`trailer`), exempting the commits listed in the gateway file
    `baseline`. Empty values are written as they are, and git-gate refuses
    pushes over them. Run in the gateway at registration."""
    if not _SAFE_BOTTLE_ID.fullmatch(bottle_id):
        raise ValueError(f"git-gate: unsafe bottle id {bottle_id!r}")
    if trailer_mode and trailer_mode not in TRAILER_MODES:
        raise ValueError(f"git-gate: unknown trailer mode {trailer_mode!r}")
    if trailer and not TRAILER_VALUE_RE.fullmatch(trailer):
        raise ValueError(f"git-gate: unsafe trailer value {trailer!r}")
    if baseline != f"/git-gate/creds/{bottle_id}/baseline-commits":
        raise ValueError(f"git-gate: unexpected baseline path {baseline!r}")
    repo_root = f"/git/{bottle_id}"
    creds_dir = f"/git-gate/creds/{bottle_id}"
    lines = ["#!/bin/sh", "set -eu", ""]
    lines += _git_gate_init_repo_fn(repo_root, creds_dir)
    lines += ["", f"trailers_mode={shlex.quote(trailer_mode)}",
              f"trailer={shlex.quote(trailer)}",
              f"baseline={shlex.quote(baseline)}",
              f"mkdir -p {shlex.quote(repo_root)}"]
    for u in upstreams:
        token = (f"/dev/shm/git-gate/{bottle_id}/{u.token_secret}-token"
                 if u.transport == "https" else "")
        if token and not _SAFE_BOTTLE_ID.fullmatch(u.token_secret):
            raise ValueError(f"git-gate: unsafe token secret name {u.token_secret!r}")
        lines.append(f"init_repo {shlex.quote(u.name)} {shlex.quote(u.upstream_url)} "
                     f"{'false' if u.writable else 'true'} "
                     f"{'https' if u.transport == 'https' else 'ssh'} {shlex.quote(token)}")
    return "\n".join(lines) + "\n"


def git_gate_render_hook() -> str:
    """The shared pre-receive hook (POSIX sh): check every new commit carries
    the bottle's `Bot-Bottle` trailer, gitleaks-scan every incoming
    ref, refuse ones carrying a secret the bottle holds, then forward each ref
    to `origin` with the repo's credential. Every ref is scanned before any is
    forwarded. stdin is saved to a temp file, since it can be read only once."""
    return r"""#!/bin/sh
# git-gate pre-receive. Stdin: <old> <new> <ref> per line.
set -u

refs_file=$(mktemp)
trap 'rm -f "$refs_file"' EXIT
cat > "$refs_file"

zero=0000000000000000000000000000000000000000

# A read-only grant: the deploy key can't push, so refuse before scanning.
if [ "$(git config --bool git-gate.readOnly 2>/dev/null)" = "true" ]; then
  echo "git-gate: this repo is granted read-only to the bottle; pushing needs" >&2
  echo "git-gate: a :write grant (ask the operator to change the bottle)" >&2
  exit 1
fi

# Record a push decision in the audit journal. Best effort: it never fails
# or delays the push beyond its short send timeout.
audit_push() {
  python3 -m bot_bottle.gateway.git_gate.audit "$@" >/dev/null 2>&1 || :
}

# The bottle's audit.delivery. Under `required` each ref is recorded before
# it's forwarded, and the push is refused if the record can't be written.
audit_delivery=$(python3 -m bot_bottle.gateway.git_gate.audit delivery 2>/dev/null || echo required)

# Phase 0: reject Gitea AGit review refs before scanning or forwarding.
# A push to refs/for/*, refs/draft/*, or refs/for-review/* asks Gitea to
# open a pull request backed by a server-managed refs/pull/<n>/head rather
# than an ordinary refs/heads/* branch. That breaks the git-gate workflow:
# follow-up commits can't be pushed back through the branch, and Gitea
# rejects later direct updates to the generated review ref. Fail the whole
# push here (before any gitleaks scan or upstream forward) so the caller
# pushes a real branch and opens the PR against it instead. Deletions
# (new == zero) stay allowed so stale AGit refs can still be cleaned up.
while IFS=' ' read -r old new ref; do
  [ -z "$ref" ] && continue
  [ "$new" = "$zero" ] && continue
  case "$ref" in
    refs/for/*|refs/draft/*|refs/for-review/*)
      echo "git-gate: refusing AGit review ref $ref" >&2
      echo "git-gate: push to refs/heads/<branch> and open a branch-backed pull request instead" >&2
      audit_push forge.push_rejected blocked "$ref" --detail "AGit review ref"
      exit 1
      ;;
  esac
done < "$refs_file"

# Phase 0b: every new commit carries this bottle's Bot-Bottle trailer
# (git-gate.trailers: require refuses, warn only says so, off skips; a repo
# missing its trailer settings refuses every push).
# Rejected, never rewritten: rewriting would change the agent's hashes.
if ! PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m bot_bottle.gateway.git_gate.trailer_check "$refs_file"; then
  audit_push forge.push_rejected blocked "*" --detail "commits without this bottle's Bot-Bottle trailer"
  exit 1
fi

supervise_gitleaks_allow() {
  log_opts=$1
  ref=$2
  report_file=$(mktemp)
  if ! gitleaks git \
      --config /etc/git-gate/gitleaks.toml \
      --log-opts="$log_opts" \
      --no-banner \
      --redact \
      --ignore-gitleaks-allow \
      --report-format=json \
      --report-path="$report_file" \
      --exit-code 0 \
      1>&2; then
    rm -f "$report_file"
    echo "git-gate: gitleaks inline-suppression scan failed for $ref" >&2
    return 1
  fi

  proposal_id=$(
    PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" GITLEAKS_ALLOW_REF="$ref" python3 - "$report_file" <<'PY'
import json
import os
import sys
from pathlib import Path

# Queue over the control-plane RPC — the git-gate data plane never opens
# bot-bottle.db. Attribution is by (source_ip, identity_token), resolved server-side, so the proposal lands under the
# calling bottle exactly as a direct write once did.
try:
    from bot_bottle.gateway.policy_resolver import PolicyResolver, PolicyResolveError
    from bot_bottle.contract.supervise import TOOL_GITLEAKS_ALLOW
except ImportError:
    from policy_resolver import PolicyResolver, PolicyResolveError
    from contract.supervise import TOOL_GITLEAKS_ALLOW

report_path = Path(sys.argv[1])
source_ip = os.environ.get("SUPERVISE_SOURCE_IP", "")
token = os.environ.get("SUPERVISE_IDENTITY_TOKEN", "")
orch_url = os.environ.get("BOT_BOTTLE_ORCHESTRATOR_URL", "")
if not source_ip or not orch_url:
    sys.exit(2)

try:
    raw = json.loads(report_path.read_text() or "[]")
except json.JSONDecodeError:
    sys.exit(3)
if not isinstance(raw, list):
    sys.exit(3)
if not raw:
    sys.exit(0)

ref = os.environ.get("GITLEAKS_ALLOW_REF", "")
lines = [
    "gitleaks inline suppression requires supervisor approval",
    f"ref: {ref}",
    "",
]
for i, finding in enumerate(raw, 1):
    if not isinstance(finding, dict):
        continue
    file_path = finding.get("File", "")
    line_no = finding.get("StartLine", finding.get("Line", ""))
    rule_id = finding.get("RuleID", "")
    commit = finding.get("Commit", "")
    line = finding.get("Line", "")
    lines.extend([
        f"finding {i}:",
        f"  file: {file_path}",
        f"  line: {line_no}",
        f"  rule: {rule_id}",
        f"  commit: {commit}",
        f"  code: {line}",
        "",
    ])

payload = "\n".join(lines).rstrip() + "\n"
try:
    proposal_id = PolicyResolver(orch_url).propose_supervise(
        source_ip, token,
        tool=TOOL_GITLEAKS_ALLOW,
        proposed_file=payload,
        justification=(
            "git-gate found gitleaks findings hidden by # gitleaks:allow; "
            "approve only for dummy test fixtures or confirmed false positives"
        ),
    )
except PolicyResolveError:
    sys.exit(4)
if not proposal_id:
    sys.exit(2)
print(proposal_id)
PY
  )
  rc=$?
  rm -f "$report_file"
  if [ "$rc" -eq 0 ] && [ -z "$proposal_id" ]; then
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    echo "git-gate: cannot route # gitleaks:allow finding to supervisor; refusing push" >&2
    return 1
  fi

  timeout=${SUPERVISE_GITLEAKS_ALLOW_TIMEOUT_SECONDS:-300}
  case "$timeout" in
    ''|*[!0-9]*)
      echo "git-gate: invalid SUPERVISE_GITLEAKS_ALLOW_TIMEOUT_SECONDS=$timeout" >&2
      return 1
      ;;
  esac
  echo "git-gate: queued # gitleaks:allow supervisor approval $proposal_id" >&2
  echo "git-gate: approve with 'bot-bottle supervise' to continue this push" >&2
  waited=0
  while [ "$waited" -lt "$timeout" ]; do
    status=$(PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" python3 - "$proposal_id" <<'PY'
import os
import sys

# Non-blocking poll over the control plane. A decided proposal is archived
# server-side on read, so no separate archive step is needed here.
try:
    from bot_bottle.gateway.policy_resolver import PolicyResolver, PolicyResolveError
except ImportError:
    from policy_resolver import PolicyResolver, PolicyResolveError

source_ip = os.environ.get("SUPERVISE_SOURCE_IP", "")
token = os.environ.get("SUPERVISE_IDENTITY_TOKEN", "")
orch_url = os.environ.get("BOT_BOTTLE_ORCHESTRATOR_URL", "")
try:
    result = PolicyResolver(orch_url).poll_supervise(source_ip, token, sys.argv[1])
except PolicyResolveError:
    sys.exit(2)
if result is None:
    sys.exit(2)
status = result.get("status", "")
if status in ("pending", "unknown"):
    sys.exit(2)
print(status)
PY
    )
    rc=$?
    if [ "$rc" -eq 2 ]; then
      status=""
    elif [ "$rc" -ne 0 ]; then
      status="invalid"
    fi
    if [ -n "$status" ]; then
      case "$status" in
        approved|modified)
          echo "git-gate: supervisor approved # gitleaks:allow for $ref" >&2
          return 0
          ;;
        rejected)
          echo "git-gate: supervisor rejected # gitleaks:allow for $ref" >&2
          return 1
          ;;
        *)
          echo "git-gate: invalid supervisor response for # gitleaks:allow" >&2
          return 1
          ;;
      esac
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "git-gate: supervisor approval timed out for # gitleaks:allow; refusing push" >&2
  return 1
}

# Phase 1: gitleaks scan each ref's incoming commits.
while IFS=' ' read -r old new ref; do
  [ -z "$ref" ] && continue
  [ "$new" = "$zero" ] && continue
  # Scan only the commits this push introduces — those reachable from
  # $new but not from any ref the gate already has. Everything already
  # on the gate arrived via upstream mirror-fetch or a previously
  # gitleaks-scanned push, so it's already-upstream or already-scanned;
  # re-scanning it only resurfaces historical fixture findings.
  #
  # Applies to both new refs and updates. The old existing-branch range
  # `$old..$new` walks commits reachable from the new tip but not the
  # *old branch tip*: on a rebase/force-push onto a freshly-advanced
  # main that pulls in all of main's new history (incl. the deliberate
  # sandbox-escape gitleaks fixtures), blocking the push. `--not --all`
  # excludes anything already on the gate regardless of ancestry, so it
  # is also correct for non-fast-forward pushes (a rebase can skip
  # commits off the direct path). Security-equivalent because the bare
  # repo's refs come only from trusted upstream mirror-fetch or
  # gitleaks-gated pushes; see docs/reference/git-gate.md.
  log_opts="$new --not --all"
  echo "git-gate: gitleaks scanning $ref ($log_opts)" >&2
  scan_report=$(mktemp)
  if ! gitleaks git --config /etc/git-gate/gitleaks.toml --log-opts="$log_opts" --no-banner --redact \
      --report-format=json --report-path="$scan_report" 1>&2; then
    echo "git-gate: gitleaks rejected push to $ref" >&2
    audit_push forge.push_rejected blocked "$ref" --report "$scan_report" --detail "gitleaks findings"
    rm -f "$scan_report"
    exit 1
  fi
  rm -f "$scan_report"
  if ! supervise_gitleaks_allow "$log_opts" "$ref"; then
    audit_push forge.push_rejected blocked "$ref" --detail "gitleaks:allow finding not approved"
    exit 1
  fi
done < "$refs_file"

# Phase 1b: refuse a push carrying a secret this bottle holds (its canary,
# its secret env values, its egress tokens). gitleaks only knows secret
# *shapes*; this knows the bottle's actual values. Fails closed.
if ! PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m bot_bottle.gateway.git_gate.held_secret_scan "$refs_file"; then
  audit_push forge.push_rejected blocked "*" --detail "carries a secret this bottle holds"
  exit 1
fi

# Phase 2: forward each ref to the upstream (`origin`, configured
# in the entrypoint via `git remote add --mirror=fetch`).
transport=$(git config --get git-gate.transport || echo ssh)
if [ "$transport" = https ]; then
  # A GitHub repo over HTTPS with the user's token (ADR 0014), read from the
  # gateway's tmpfs per push; the header is only ever in this subshell's env.
  token_file=$(git config --get git-gate.tokenFile || :)
  if [ -z "$token_file" ] || [ ! -s "$token_file" ]; then
    echo "git-gate: no GitHub token for this repo in the gateway; refusing to push" >&2
    echo "git-gate: reconnect GitHub on the host (bot-bottle forge connect github)" >&2
    audit_push forge.push_rejected failure "*" --detail "no GitHub token for the upstream"
    exit 1
  fi
  auth=$(printf 'x-access-token:%s' "$(cat "$token_file")" | base64 | tr -d '\n')
  upstream_git() (
    export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 \
      GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: Basic $auth" \
      GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1=
    exec git "$@"
  )
else
  keyfile=$(git config --get git-gate.identityFile)
  hostsfile=$(git config --get git-gate.knownHosts)
  if [ ! -f "$hostsfile" ]; then
    echo "git-gate: no KnownHostKey configured for this upstream; refusing to push" >&2
    echo "git-gate: add KnownHostKey to the bottle.git entry and restart the bottle" >&2
    audit_push forge.push_rejected failure "*" --detail "no KnownHostKey for the upstream"
    exit 1
  fi
  ssh_cmd="ssh -i $keyfile -o UserKnownHostsFile=$hostsfile -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10"
  upstream_git() { GIT_SSH_COMMAND="$ssh_cmd" git "$@"; }
fi
# What to tell the agent when GitHub refuses (HTTPS only).
upstream_hint() {
  [ "$transport" = https ] || return 0
  if grep -qiE "Invalid username or (password|token)|Authentication failed|returned error: 401" "$1"; then
    expires=$(cat "${token_file}.expires" 2>/dev/null || :)
    case "$expires" in ''|*[!0-9]*) expires= ;; esac
    if [ -n "$expires" ] && [ "$(date +%s)" -ge "$expires" ]; then
      echo "git-gate: git-gate's GitHub token expired and wasn't renewed: the host controller renews it every few minutes, so if it has been down, start it again (bot-bottle up)" >&2
    elif [ -n "$expires" ]; then
      echo "git-gate: GitHub refused a token that hasn't expired, so the authorization was revoked; re-authorize GitHub on the host (bot-bottle forge connect github --reauthorize)" >&2
    else
      echo "git-gate: GitHub refused the token: if the host controller has been down it may have expired (start it again: bot-bottle up), otherwise re-authorize GitHub on the host (bot-bottle forge connect github --reauthorize)" >&2
    fi
  elif grep -qiE "Permission to .* denied|Write access to repository not granted|returned error: 403" "$1"; then
    echo "git-gate: GitHub refused the push; the bot-bottle app needs Contents: read & write and must be installed on the repo's owner" >&2
  elif grep -qiE "Repository not found|returned error: 404" "$1"; then
    echo "git-gate: GitHub can't see the repo with this token; install the bot-bottle app on its owner" >&2
  fi
}

push_option_count=${GIT_PUSH_OPTION_COUNT:-0}
case "$push_option_count" in
  ''|*[!0-9]*)
    echo "git-gate: invalid GIT_PUSH_OPTION_COUNT=$push_option_count" >&2
    exit 1
    ;;
esac
set --
i=0
while [ "$i" -lt "$push_option_count" ]; do
  opt=$(printenv "GIT_PUSH_OPTION_$i" || :)
  set -- "$@" --push-option="$opt"
  i=$((i + 1))
done

while IFS=' ' read -r old new ref; do
  [ -z "$ref" ] && continue
  if [ "$new" = "$zero" ]; then
    refspec=":$ref"
  elif [ "$old" != "$zero" ] && ! git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
    refspec="+$new:$ref"
  else
    refspec="$new:$ref"
  fi
  if [ "$new" = "$zero" ]; then
    accepted="--detail ref-deleted"
  else
    accepted="--findings 0 --new $new"
  fi
  if [ "$audit_delivery" = required ]; then
    # shellcheck disable=SC2086 # $accepted is several words on purpose
    if ! python3 -m bot_bottle.gateway.git_gate.audit --required \
        forge.push_accepted success "$ref" $accepted 1>&2; then
      echo "git-gate: refusing push to $ref: its audit record could not be written" >&2
      exit 1
    fi
  fi
  echo "git-gate: forwarding $ref to origin" >&2
  # To origin's URL, not the remote: pushing to a mirror remote also updates
  # the matching local ref, which git forbids inside pre-receive's quarantine
  # ("update_ref failed ... inside quarantine environment"). The next fetch
  # brings the ref in.
  upstream_url=$(git config --get remote.origin.url)
  push_err=$(mktemp)
  if ! upstream_git push "$@" "$upstream_url" "$refspec" 1>&2 2>"$push_err"; then
    cat "$push_err" >&2
    upstream_hint "$push_err"
    rm -f "$push_err"
    echo "git-gate: upstream push failed for $ref" >&2
    audit_push forge.push_rejected failure "$ref" --detail "upstream push failed"
    exit 1
  fi
  cat "$push_err" >&2
  rm -f "$push_err"
  if [ "$audit_delivery" != required ]; then
    # shellcheck disable=SC2086
    audit_push forge.push_accepted success "$ref" $accepted
  fi
done < "$refs_file"

exit 0
"""


def _gitleaks_literal_prefix(pattern_str: str) -> str:
    """The longest leading literal of a regex, lowercased, as a gitleaks
    keyword. Stops at the first metacharacter; `\\.` contributes `.`, while
    `\\s` ends it."""
    result: list[str] = []
    i = 0
    while i < len(pattern_str):
        ch = pattern_str[i]
        if ch == "\\" and i + 1 < len(pattern_str):
            next_ch = pattern_str[i + 1]
            if next_ch in ("s", "S", "d", "D", "w", "W"):
                break
            result.append(next_ch)
            i += 2
        elif ch in ("[", "{", "(", "?", "*", "+", "|", "^", "$", "."):
            break
        else:
            result.append(ch)
            i += 1
    return "".join(result).lower()


def _gitleaks_rule_id(name: str) -> str:
    """A `dlp-`-prefixed gitleaks rule id for a DLP detector name."""
    return "dlp-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def git_gate_render_gitleaks_config() -> str:
    """A gitleaks TOML that keeps the default rules (`useDefault = true`) and
    adds each egress DLP token pattern without a context keyword, so pushes get
    the same token-shape coverage as egress. Installed at
    `/etc/git-gate/gitleaks.toml`."""
    lines = [
        "# Auto-generated from TOKEN_PATTERNS in dlp_detectors.py — edit there.",
        "[extend]",
        "useDefault = true",
        "",
    ]
    for name, pattern in TOKEN_PATTERNS:
        rule_id = _gitleaks_rule_id(name)
        keyword = _gitleaks_literal_prefix(pattern.pattern)
        lines += [
            "[[rules]]",
            f'id = "{rule_id}"',
            f'description = "{name} (DLP mirror)"',
            f"regex = '''{pattern.pattern}'''",
        ]
        if keyword:
            lines.append(f'keywords = ["{keyword}"]')
        lines.append("")
    return "\n".join(lines)


def git_gate_render_access_hook() -> str:
    """The access hook (POSIX sh). Before a fetch (`upload-pack`) and at the
    start of a push (`receive-pack`'s ref advertisement) it refreshes the bare
    repo from upstream, failing if the upstream is unreachable; the push itself
    is gated by pre-receive.

      $1  service name    $2  repo path    $3, $4  client host and address"""
    return r"""#!/bin/sh
# git-gate access-hook. $1=service $2=repo $3=host $4=peer
set -u
service=$1
repo_dir=$2

# Refresh before a fetch, and at the start of a push (its ref
# advertisement) so pre-receive's scan only covers commits upstream lacks.
# The push itself keeps its own gating in pre-receive.
case "$service" in
  upload-pack|receive-pack) ;;
  *) exit 0 ;;
esac

transport=$(git -C "$repo_dir" config --get git-gate.transport 2>/dev/null || echo ssh)
if [ "$transport" = https ]; then
  token_file=$(git -C "$repo_dir" config --get git-gate.tokenFile 2>/dev/null || true)
  if [ -z "$token_file" ] || [ ! -s "$token_file" ]; then
    echo "git-gate: no GitHub token for $repo_dir in the gateway; refusing fetch" >&2
    echo "git-gate: reconnect GitHub on the host (bot-bottle forge connect github)" >&2
    exit 1
  fi
  auth=$(printf 'x-access-token:%s' "$(cat "$token_file")" | base64 | tr -d '\n')
  upstream_git() (
    export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 \
      GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: Basic $auth" \
      GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1=
    exec git "$@"
  )
else
  keyfile=$(git -C "$repo_dir" config --get git-gate.identityFile 2>/dev/null || true)
  hostsfile=$(git -C "$repo_dir" config --get git-gate.knownHosts 2>/dev/null || true)
  if [ -z "$keyfile" ] || [ ! -f "$hostsfile" ]; then
    echo "git-gate: missing credentials for $repo_dir; refusing fetch" >&2
    exit 1
  fi
  ssh_cmd="ssh -i $keyfile -o UserKnownHostsFile=$hostsfile -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10"
  upstream_git() { GIT_SSH_COMMAND="$ssh_cmd" git "$@"; }
fi

echo "git-gate: refreshing $repo_dir from upstream" >&2
if ! upstream_git -C "$repo_dir" fetch origin --prune >&2; then
  if [ "$service" = "receive-pack" ]; then
    # Best effort for a push: pre-receive still scans it (over more history)
    # and its forward fails if upstream is really down.
    echo "git-gate: upstream fetch failed for $repo_dir; scanning the push anyway" >&2
    exit 0
  fi
  echo "git-gate: upstream fetch failed for $repo_dir; refusing to serve stale data" >&2
  exit 1
fi

# Sync the bare repo's HEAD to upstream's HEAD on the first fetch
# (when it still points at the `git init --bare` default of
# refs/heads/master and upstream uses something else, the cloned
# checkout would fail with "remote HEAD refers to nonexistent ref").
# Costs one extra ls-remote on first fetch only; subsequent fetches
# skip the branch. If upstream's default branch changes after the
# gate has cached it, restart the bottle to resync.
if ! git -C "$repo_dir" rev-parse --verify HEAD >/dev/null 2>&1; then
  upstream_head=$(upstream_git -C "$repo_dir" \
    ls-remote --symref origin HEAD 2>/dev/null \
    | awk '/^ref:/ {print $2; exit}')
  if [ -n "$upstream_head" ]; then
    git -C "$repo_dir" symbolic-ref HEAD "$upstream_head" || true
  fi
fi
exit 0
"""
