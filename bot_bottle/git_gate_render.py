"""Pure host-side rendering for the per-agent git-gate (PRD 0008).

Builds the agent's `.gitconfig` insteadOf rewrites, the known_hosts
line, and the entrypoint / pre-receive / access-hook scripts the gateway
runs. No docker or forge calls — exposed for tests and reuse across
backends. Split out of `git_gate.py` so the control surface (`GitGate`)
and the deploy-key lifecycle (`git_gate_provision`) each read on their
own; `git_gate` re-exports these names for API stability."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .manifest import ManifestBottle, ManifestGitEntry

# Short network alias for git-gate inside the gateway. The
# agent's `.gitconfig` insteadOf rewrites resolve through this name.
GIT_GATE_HOSTNAME = "git-gate"
# Shared timeout (seconds) for all git-gate subprocess and CGI calls:
# git daemon (--timeout/--init-timeout), the access-hook subprocess in
# git_http_backend, and the git http-backend CGI subprocess.
GIT_GATE_TIMEOUT_SECS = 15


@dataclass(frozen=True)
class GitGateUpstream:
    """One bare repo on the gate. `name` drives the bare-repo path
    (`/git/<name>.git`), the agent's URL after insteadOf rewrite
    (`git://<gate>/<name>.git`), and the per-upstream credential
    paths inside the gate (`/git-gate/creds/<name>-key` and
    `/git-gate/creds/<name>-known_hosts`).

    `identity_file` is the host-side absolute path the gate's start
    step will docker-cp into the container. `known_host_key` is the
    KnownHostKey string from the manifest; the gate's start step
    materialises it into a known_hosts file if non-empty.

    the gate credential paths inside the running gateway."""

    name: str
    upstream_url: str
    upstream_host: str
    upstream_port: str
    identity_file: str
    known_host_key: str
    known_hosts_file: Path = Path()

def git_gate_upstreams_for_bottle(bottle: ManifestBottle) -> tuple[GitGateUpstream, ...]:
    """Lift each `bottle.git` entry into a GitGateUpstream. Unique-Name
    validation already ran in `manifest.ManifestBottle.from_dict`."""
    return tuple(
        GitGateUpstream(
            name=e.Name,
            upstream_url=e.Upstream,
            upstream_host=e.UpstreamHost,
            upstream_port=e.UpstreamPort,
            identity_file=e.IdentityFile,
            known_host_key=e.KnownHostKey,
        )
        for e in bottle.git
    )


def _gitconfig_validate_value(field: str, value: str) -> None:
    """Raise ValueError if value contains characters that break gitconfig line syntax."""
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"git-gate: {field} contains a newline, which would inject "
            f"arbitrary gitconfig keys; rejecting manifest entry"
        )


def git_gate_render_gitconfig(
    entries: tuple[ManifestGitEntry, ...], gate_host: str, *, scheme: str = "git",
) -> str:
    """Render the agent's ~/.gitconfig content for git-gate
    `insteadOf` rewrites. Pure host-side, no docker / VM;
    exposed for tests + reuse across backends.

    `gate_host` is the part of the URL between `<scheme>://` and the
    repo path — backends differ here:
      - docker:        `git-gate` (the short network alias)
      - firecracker:   `<bundle_ip>:<port>` (no DNS on the
                       point-to-point TAP link)

    Empty `entries` returns an empty string so callers can no-op
    cleanly without conditional formatting at the call site."""
    if not entries:
        return ""
    out = [
        "# bot-bottle git-gate (PRD 0008): every git operation against\n",
        "# a declared upstream routes through the gate, which mirrors\n",
        "# the upstream bidirectionally (gitleaks-scanned push;\n",
        "# fetch-from-upstream-before-every-upload-pack via access-hook).\n",
    ]
    for entry in entries:
        _gitconfig_validate_value(f"repos[{entry.Name!r}].url", entry.Upstream)
        out.append(f'[url "{scheme}://{gate_host}/{entry.Name}.git"]\n')
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
    """Format `host[:port] key` for OpenSSH's known_hosts. Non-default
    ports use the bracketed `[host]:port` form (the form OpenSSH writes
    on disk for hosts reached via a non-22 port)."""
    if port and port != "22":
        target = f"[{host}]:{port}"
    else:
        target = host
    return f"{target} {key}\n"


def _git_gate_init_repo_fn(repo_root: str, creds_dir: str) -> list[str]:
    """The `init_repo` shell function, parameterized by the bare-repo root
    and the per-bottle creds dir. Single source of the credential-wiring
    logic, shared by the single-tenant daemon entrypoint (`/git`,
    `/git-gate/creds`) and the consolidated per-bottle provisioning
    (`/git/<bottle_id>`, `/git-gate/creds/<bottle_id>`)."""
    return [
        "init_repo() {",
        "  name=$1",
        "  upstream_url=$2",
        f"  keyfile={creds_dir}/${{name}}-key",
        f"  hostsfile={creds_dir}/${{name}}-known_hosts",
        "",
        # `|| true`: PRD 0018 chunk 3+ bind-mounts these RO from the
        # host, so chmod-syscalls fail with EROFS. The files already
        # have the right perms on the host (SSH requires 0600 to load
        # the key in the first place), so the chmod is best-effort
        # cleanup for the legacy docker-cp path where the file
        # landed at the host's umask perms.
        "  chmod 600 \"$keyfile\" 2>/dev/null || true",
        "  if [ -f \"$hostsfile\" ]; then",
        "    chmod 600 \"$hostsfile\" 2>/dev/null || true",
        "  fi",
        "",
        f"  repo={repo_root}/${{name}}.git",
        "  if [ ! -d \"$repo\" ]; then",
        "    git init --bare \"$repo\" >/dev/null",
        # --mirror=fetch sets remote.origin.fetch = +refs/*:refs/* so a later
        # `git fetch origin` mirrors the upstream's full ref graph (heads,
        # tags, notes) into the bare repo at canonical paths. It does NOT set
        # remote.origin.mirror=true, so an explicit `git push origin
        # <ref>:<ref>` still pushes one ref.
        "    git -C \"$repo\" remote add --mirror=fetch origin \"$upstream_url\"",
        "  fi",
        "  git -C \"$repo\" config git-gate.identityFile \"$keyfile\"",
        "  git -C \"$repo\" config git-gate.knownHosts \"$hostsfile\"",
        "  git -C \"$repo\" config receive.denyCurrentBranch ignore",
        "  git -C \"$repo\" config receive.advertisePushOptions true",
        "  git -C \"$repo\" config http.receivepack true",
        "  install -m 755 /etc/git-gate/pre-receive \"$repo/hooks/pre-receive\"",
        "}",
    ]


def git_gate_render_entrypoint(upstreams: tuple[GitGateUpstream, ...]) -> str:
    """Posix-sh entrypoint. One `init_repo` call per upstream, then
    `exec git daemon`. The function reads
    `/git-gate/creds/<name>-{key,known_hosts}` (bind-mounted into
    the bundle by the renderer) and wires them into each bare repo's
    config; the access-hook + pre-receive hook pick those paths up
    at fetch / push time."""
    lines = ["#!/bin/sh", "set -eu", ""]
    lines += _git_gate_init_repo_fn("/git", "/git-gate/creds")
    lines += ["", "mkdir -p /git"]
    for u in upstreams:
        lines.append(f"init_repo {shlex.quote(u.name)} {shlex.quote(u.upstream_url)}")
    lines.extend([
        "",
        "exec git daemon \\",
        "  --reuseaddr \\",
        f"  --timeout={GIT_GATE_TIMEOUT_SECS} \\",
        f"  --init-timeout={GIT_GATE_TIMEOUT_SECS} \\",
        "  --base-path=/git \\",
        "  --export-all \\",
        "  --enable=receive-pack \\",
        "  --access-hook=/etc/git-gate/access-hook \\",
        "  --verbose",
    ])
    return "\n".join(lines) + "\n"


# A bottle id namespaces the consolidated gateway's repo + creds dirs; it is
# embedded unquoted in the provisioning script, so restrict it to a shell- and
# path-safe alphabet (registry ids are token_hex — this is defense in depth).
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


def git_gate_render_provision(
    bottle_id: str, upstreams: tuple[GitGateUpstream, ...],
) -> str:
    """Posix-sh script that provisions ONE bottle's bare repos into the
    consolidated gateway (PRD 0070), under `/git/<bottle_id>/` with creds
    read from `/git-gate/creds/<bottle_id>/`. Init-only — no `git daemon`,
    since the shared gateway already serves every bottle; run inside the
    running gateway when the bottle is registered.

    Isolating each bottle's repo root and creds dir by id is what keeps one
    bottle's push credentials out of another's repos on the shared gateway."""
    if not _SAFE_BOTTLE_ID.fullmatch(bottle_id):
        raise ValueError(f"git-gate: unsafe bottle id {bottle_id!r}")
    repo_root = f"/git/{bottle_id}"
    creds_dir = f"/git-gate/creds/{bottle_id}"
    lines = ["#!/bin/sh", "set -eu", ""]
    lines += _git_gate_init_repo_fn(repo_root, creds_dir)
    lines += ["", f"mkdir -p {shlex.quote(repo_root)}"]
    for u in upstreams:
        lines.append(f"init_repo {shlex.quote(u.name)} {shlex.quote(u.upstream_url)}")
    return "\n".join(lines) + "\n"


def git_gate_render_hook() -> str:
    """The shared pre-receive hook: gitleaks-scan all incoming refs,
    then forward each accepted ref to the real upstream (`origin`)
    using the per-repo credential. Failure in either phase aborts
    the push so the agent sees a real rejection. POSIX sh.

    Two phases (scan all, then push all) keeps a hit on ref N from
    half-pushing refs 1..N-1; both phases re-read stdin from a temp
    file because pre-receive's stdin is a one-shot stream."""
    return r"""#!/bin/sh
# git-gate pre-receive (PRD 0008). Stdin: <old> <new> <ref> per line.
set -u

refs_file=$(mktemp)
trap 'rm -f "$refs_file"' EXIT
cat > "$refs_file"

zero=0000000000000000000000000000000000000000

supervise_gitleaks_allow() {
  log_opts=$1
  ref=$2
  report_file=$(mktemp)
  if ! gitleaks git \
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
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    import supervise as _sv
except ImportError:
    from bot_bottle import supervise as _sv

report_path = Path(sys.argv[1])
slug = os.environ.get("SUPERVISE_BOTTLE_SLUG", "")
if not slug:
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
proposal = _sv.Proposal.new(
    bottle_slug=slug,
    tool=_sv.TOOL_GITLEAKS_ALLOW,
    proposed_file=payload,
    justification=(
        "git-gate found gitleaks findings hidden by # gitleaks:allow; "
        "approve only for dummy test fixtures or confirmed false positives"
    ),
    current_file_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    now=datetime.datetime.now(datetime.timezone.utc),
)
_sv.write_proposal(proposal)
print(proposal.id)
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

  slug=${SUPERVISE_BOTTLE_SLUG:-}
  timeout=${SUPERVISE_GITLEAKS_ALLOW_TIMEOUT_SECONDS:-300}
  case "$timeout" in
    ''|*[!0-9]*)
      echo "git-gate: invalid SUPERVISE_GITLEAKS_ALLOW_TIMEOUT_SECONDS=$timeout" >&2
      return 1
      ;;
  esac
  echo "git-gate: queued # gitleaks:allow supervisor approval $proposal_id" >&2
  echo "git-gate: approve with './cli.py supervise' to continue this push" >&2
  waited=0
  while [ "$waited" -lt "$timeout" ]; do
    status=$(PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" python3 - "$slug" "$proposal_id" <<'PY'
import sys

try:
    import supervise as _sv
except ImportError:
    from bot_bottle import supervise as _sv

slug = sys.argv[1]
try:
    response = _sv.read_response(slug, sys.argv[2])
except FileNotFoundError:
    sys.exit(2)
print(response.status)
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
          PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}" python3 - "$slug" "$proposal_id" <<'PY' || true
import sys

try:
    import supervise as _sv
except ImportError:
    from bot_bottle import supervise as _sv

_sv.archive_proposal(sys.argv[1], sys.argv[2])
PY
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
  if [ "$old" = "$zero" ]; then
    # New ref: scan only the commits this push introduces — those
    # reachable from $new but not from any ref the gate already has.
    # Everything already on the gate arrived via upstream mirror-fetch
    # or a previously gitleaks-scanned push, so it's already-upstream
    # or already-scanned; re-scanning it (the old `$new` full-ancestry
    # range) only resurfaces historical findings and blocks every new
    # branch. See PRD 0028 / issue #106.
    log_opts="$new --not --all"
  else
    log_opts="$old..$new"
  fi
  echo "git-gate: gitleaks scanning $ref ($log_opts)" >&2
  if ! gitleaks git --log-opts="$log_opts" --no-banner --redact 1>&2; then
    echo "git-gate: gitleaks rejected push to $ref" >&2
    exit 1
  fi
  if ! supervise_gitleaks_allow "$log_opts" "$ref"; then
    exit 1
  fi
done < "$refs_file"

# Phase 2: forward each ref to the upstream (`origin`, configured
# in the entrypoint via `git remote add --mirror=fetch`).
keyfile=$(git config --get git-gate.identityFile)
hostsfile=$(git config --get git-gate.knownHosts)
if [ ! -f "$hostsfile" ]; then
  echo "git-gate: no KnownHostKey configured for this upstream; refusing to push" >&2
  echo "git-gate: add KnownHostKey to the bottle.git entry and restart the bottle" >&2
  exit 1
fi
ssh_cmd="ssh -i $keyfile -o UserKnownHostsFile=$hostsfile -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10"

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
  echo "git-gate: forwarding $ref to origin" >&2
  if ! GIT_SSH_COMMAND="$ssh_cmd" git push "$@" origin "$refspec" 1>&2; then
    echo "git-gate: upstream push failed for $ref" >&2
    exit 1
  fi
done < "$refs_file"

exit 0
"""


def git_gate_render_access_hook() -> str:
    """`git daemon --access-hook` script. Runs before each protocol
    service; for `upload-pack` (fetch / clone / ls-remote / pull) it
    refreshes the bare repo from upstream first, so the response
    reflects upstream's current state. For other services (notably
    `receive-pack`) it returns 0 immediately and lets the existing
    pre-receive hook gate the operation. POSIX sh.

    The hook receives:
      $1  service name (`upload-pack`, `receive-pack`, ...)
      $2  absolute path to the resolved repo
      $3  client hostname (unused)
      $4  client tcp address (unused)

    Fail-closed on upstream errors: the agent's fetch fails too,
    so it never silently sees stale data — matches the PRD's
    'equivalent to operations against the upstream' contract."""
    return r"""#!/bin/sh
# git-gate access-hook (PRD 0008). $1=service $2=repo $3=host $4=peer
set -u
service=$1
repo_dir=$2

# Push path keeps its own gating in pre-receive (gitleaks +
# forward). Only refresh-from-upstream on fetch operations.
if [ "$service" != "upload-pack" ]; then
  exit 0
fi

keyfile=$(git -C "$repo_dir" config --get git-gate.identityFile 2>/dev/null || true)
hostsfile=$(git -C "$repo_dir" config --get git-gate.knownHosts 2>/dev/null || true)
if [ -z "$keyfile" ] || [ ! -f "$hostsfile" ]; then
  echo "git-gate: missing credentials for $repo_dir; refusing fetch" >&2
  exit 1
fi
ssh_cmd="ssh -i $keyfile -o UserKnownHostsFile=$hostsfile -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10"

echo "git-gate: refreshing $repo_dir from upstream" >&2
if ! GIT_SSH_COMMAND="$ssh_cmd" git -C "$repo_dir" fetch origin --prune >&2; then
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
  upstream_head=$(GIT_SSH_COMMAND="$ssh_cmd" git -C "$repo_dir" \
    ls-remote --symref origin HEAD 2>/dev/null \
    | awk '/^ref:/ {print $2; exit}')
  if [ -n "$upstream_head" ]; then
    git -C "$repo_dir" symbolic-ref HEAD "$upstream_head" || true
  fi
fi
exit 0
"""
