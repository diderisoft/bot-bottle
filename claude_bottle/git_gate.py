"""Per-agent git-gate (PRD 0008).

A third per-agent sidecar that fronts the bottle's declared git
upstreams as a transparent mirror. Each `bottle.git` entry maps to
a bare repo on the gate; `git daemon` serves the bare repos over
`git://<gate>/<name>.git`. Two hooks make the mirror bidirectional:

- **`pre-receive`** (push path) — gitleaks-scans incoming refs and,
  on clean, forwards them to the real upstream with the
  gate-resident credential.
- **`--access-hook`** (fetch path) — runs `git fetch origin --prune`
  against the real upstream before every `upload-pack`, so an
  agent fetch returns whatever the upstream has *now*. Fail-closed
  if the upstream is unreachable.

The agent never sees the upstream credential under either path.

Why a third sidecar (not folded into pipelock or ssh-gate): the
gate is the only one of the three that holds upstream push
credentials. Mixing it with pipelock would put push creds in the
same blast radius as internet-facing TLS interception; mixing it
with ssh-gate would force ssh-gate above L4 and into git-protocol
land. See `docs/prds/0008-git-gate.md`.

This module defines the abstract gate (`GitGate`) and its plan
dataclass (`GitGatePlan`). The sidecar's start/stop lifecycle is
backend-specific and lives on concrete subclasses (see
`claude_bottle/backend/docker/git_gate.py`)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .log import die
from .manifest import Bottle, GitEntry


# Short network alias for git-gate inside the sidecar bundle. The
# agent's `.gitconfig` insteadOf rewrites resolve through this name.
GIT_GATE_HOSTNAME = "git-gate"


def _empty_str_map() -> dict[str, str]:
    return {}


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

    `extra_hosts` is a `{hostname: ip}` map the backend injects into
    the gate container's `/etc/hosts` via `--add-host` so the gate
    can resolve upstream hostnames that aren't reachable via the
    container's default DNS (e.g. Tailscale-only hosts)."""

    name: str
    upstream_url: str
    upstream_host: str
    upstream_port: str
    identity_file: str
    known_host_key: str
    extra_hosts: Mapping[str, str] = field(default_factory=_empty_str_map)


@dataclass(frozen=True)
class GitGatePlan:
    """Output of GitGate.prepare; consumed by .start.

    The script + slug + upstream fields are filled at prepare time
    (host-side, side-effect-free on docker). The network fields are
    populated by the backend's launch step via `dataclasses.replace`
    once those networks exist. Empty defaults are sentinels meaning
    "not yet set"; `.start` validates that they are populated.

    `hook_script` is the shared `pre-receive` for push-time gating;
    `access_hook_script` is `git daemon`'s `--access-hook` for the
    fetch-time upstream refresh."""

    slug: str
    entrypoint_script: Path
    hook_script: Path
    access_hook_script: Path
    upstreams: tuple[GitGateUpstream, ...]
    internal_network: str = ""
    egress_network: str = ""


def git_gate_upstreams_for_bottle(bottle: Bottle) -> tuple[GitGateUpstream, ...]:
    """Lift each `bottle.git` entry into a GitGateUpstream. Unique-Name
    validation already ran in `manifest.Bottle.from_dict`."""
    return tuple(
        GitGateUpstream(
            name=e.Name,
            upstream_url=e.Upstream,
            upstream_host=e.UpstreamHost,
            upstream_port=e.UpstreamPort,
            identity_file=e.IdentityFile,
            known_host_key=e.KnownHostKey,
            extra_hosts=dict(e.ExtraHosts),
        )
        for e in bottle.git
    )


def git_gate_aggregate_extra_hosts(
    upstreams: tuple[GitGateUpstream, ...],
) -> dict[str, str]:
    """Merge every upstream's `extra_hosts` into a single
    `{hostname: ip}` map for `--add-host` on the gate container. Two
    entries naming the same hostname with different IPs is a manifest
    bug — the gate has one /etc/hosts — so die loudly with the
    conflicting names rather than silently picking one."""
    merged: dict[str, str] = {}
    source: dict[str, str] = {}
    for u in upstreams:
        for host, ip in u.extra_hosts.items():
            existing = merged.get(host)
            if existing is None:
                merged[host] = ip
                source[host] = u.name
            elif existing != ip:
                die(
                    f"git-gate ExtraHosts conflict: '{host}' maps to "
                    f"'{existing}' in upstream '{source[host]}' and to "
                    f"'{ip}' in upstream '{u.name}'. The gate has one "
                    f"/etc/hosts; pick one IP."
                )
    return merged


def git_gate_render_gitconfig(
    entries: tuple[GitEntry, ...], gate_host: str
) -> str:
    """Render the agent's ~/.gitconfig content for git-gate
    `insteadOf` rewrites. Pure host-side, no docker / smolvm;
    exposed for tests + reuse across backends.

    `gate_host` is the part of the URL between `git://` and the
    repo path — backends differ here:
      - docker:        `git-gate` (the short network alias)
      - smolmachines:  `<bundle_ip>:<port>` (no DNS in the
                       TSI-allowlisted guest)

    Empty `entries` returns an empty string so callers can no-op
    cleanly without conditional formatting at the call site."""
    if not entries:
        return ""
    out = [
        "# claude-bottle git-gate (PRD 0008): every git operation against\n",
        "# a declared upstream routes through the gate, which mirrors\n",
        "# the upstream bidirectionally (gitleaks-scanned push;\n",
        "# fetch-from-upstream-before-every-upload-pack via access-hook).\n",
    ]
    for entry in entries:
        out.append(f'[url "git://{gate_host}/{entry.Name}.git"]\n')
        out.append(f"\tinsteadOf = {entry.Upstream}\n")
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


def git_gate_render_entrypoint(upstreams: tuple[GitGateUpstream, ...]) -> str:
    """Posix-sh entrypoint. One `init_repo` call per upstream, then
    `exec git daemon`. The function reads
    `/git-gate/creds/<name>-{key,known_hosts}` (bind-mounted into
    the bundle by the renderer) and wires them into each bare repo's
    config; the access-hook + pre-receive hook pick those paths up
    at fetch / push time."""
    lines = [
        "#!/bin/sh",
        "set -eu",
        "",
        "init_repo() {",
        "  name=$1",
        "  upstream_url=$2",
        "  keyfile=/git-gate/creds/${name}-key",
        "  hostsfile=/git-gate/creds/${name}-known_hosts",
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
        "  repo=/git/${name}.git",
        "  if [ ! -d \"$repo\" ]; then",
        "    git init --bare \"$repo\" >/dev/null",
        # --mirror=fetch sets remote.origin.fetch = +refs/*:refs/* so",
        # a later `git fetch origin` mirrors the upstream's full ref",
        # graph (heads, tags, notes) into the bare repo at canonical",
        # paths. It does NOT set remote.origin.mirror=true, so an",
        # explicit `git push origin <ref>:<ref>` still pushes one ref.",
        "    git -C \"$repo\" remote add --mirror=fetch origin \"$upstream_url\"",
        "  fi",
        "  git -C \"$repo\" config git-gate.identityFile \"$keyfile\"",
        "  git -C \"$repo\" config git-gate.knownHosts \"$hostsfile\"",
        "  git -C \"$repo\" config receive.denyCurrentBranch ignore",
        "  install -m 755 /etc/git-gate/pre-receive \"$repo/hooks/pre-receive\"",
        "}",
        "",
        "mkdir -p /git",
    ]
    for u in upstreams:
        # Single-quote args so URL/path content (containing : and /)
        # passes through ash unmangled. Names came through the manifest
        # validator so they don't contain a single quote.
        lines.append(f"init_repo '{u.name}' '{u.upstream_url}'")
    lines.extend([
        "",
        "exec git daemon \\",
        "  --reuseaddr \\",
        "  --base-path=/git \\",
        "  --export-all \\",
        "  --enable=receive-pack \\",
        "  --access-hook=/etc/git-gate/access-hook \\",
        "  --verbose",
    ])
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

# Phase 1: gitleaks scan each ref's incoming commits.
while IFS=' ' read -r old new ref; do
  [ -z "$ref" ] && continue
  [ "$new" = "$zero" ] && continue
  if [ "$old" = "$zero" ]; then
    log_opts="$new"
  else
    log_opts="$old..$new"
  fi
  echo "git-gate: gitleaks scanning $ref ($log_opts)" >&2
  if ! gitleaks git --log-opts="$log_opts" --no-banner --redact 1>&2; then
    echo "git-gate: gitleaks rejected push to $ref" >&2
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
ssh_cmd="ssh -i $keyfile -o UserKnownHostsFile=$hostsfile -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes"

while IFS=' ' read -r old new ref; do
  [ -z "$ref" ] && continue
  if [ "$new" = "$zero" ]; then
    refspec=":$ref"
  else
    refspec="$new:$ref"
  fi
  echo "git-gate: forwarding $ref to origin" >&2
  if ! GIT_SSH_COMMAND="$ssh_cmd" git push origin "$refspec" 1>&2; then
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
ssh_cmd="ssh -i $keyfile -o UserKnownHostsFile=$hostsfile -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes"

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


class GitGate(ABC):
    """The per-agent git-gate. Encapsulates the host-side prepare
    (upstream lift + entrypoint/hook render); the sidecar's
    start/stop lifecycle is backend-specific and lives on concrete
    subclasses."""

    def prepare(self, bottle: Bottle, slug: str, stage_dir: Path) -> GitGatePlan:
        """Compute the upstream table from `bottle.git` and write the
        entrypoint, pre-receive hook, and access-hook scripts (mode
        600) under `stage_dir`. Pure host-side, no docker subprocess.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` via `dataclasses.replace`
        before passing the plan to `.start`."""
        upstreams = git_gate_upstreams_for_bottle(bottle)
        entrypoint = stage_dir / "git_gate_entrypoint.sh"
        entrypoint.write_text(git_gate_render_entrypoint(upstreams))
        entrypoint.chmod(0o600)
        hook = stage_dir / "git_gate_pre_receive.sh"
        hook.write_text(git_gate_render_hook())
        hook.chmod(0o600)
        access_hook = stage_dir / "git_gate_access_hook.sh"
        access_hook.write_text(git_gate_render_access_hook())
        # 0o700 (not 0o600): git daemon execs --access-hook directly,
        # not via `sh`, so the script needs the x bit. docker cp
        # preserves source mode into the container.
        access_hook.chmod(0o700)
        return GitGatePlan(
            slug=slug,
            entrypoint_script=entrypoint,
            hook_script=hook,
            access_hook_script=access_hook,
            upstreams=upstreams,
        )

