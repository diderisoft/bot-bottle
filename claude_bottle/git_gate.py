"""Per-agent git-gate (PRD 0008).

A third per-agent sidecar that fronts the bottle's declared git
upstreams. Each `bottle.git` entry maps to a bare repo on the gate;
the gate runs `git daemon --enable=receive-pack` so the agent can
push to it via `git://<gate>/<name>.git`. A pre-receive hook scans
the incoming refs with gitleaks; on clean, it forwards the refs to
the real upstream using a credential the gate holds.

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
from dataclasses import dataclass
from pathlib import Path

from .manifest import Bottle


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
    materialises it into a known_hosts file if non-empty."""

    name: str
    upstream_url: str
    upstream_host: str
    upstream_port: str
    identity_file: str
    known_host_key: str


@dataclass(frozen=True)
class GitGatePlan:
    """Output of GitGate.prepare; consumed by .start.

    `upstreams` + `slug` + `entrypoint_script` + `hook_script` are
    filled in at prepare time (host-side, side-effect-free on docker).
    The network fields are populated by the backend's launch step via
    `dataclasses.replace` once those networks exist. Empty defaults
    are sentinels meaning "not yet set"; `.start` validates that
    they are populated."""

    slug: str
    entrypoint_script: Path
    hook_script: Path
    upstreams: tuple[GitGateUpstream, ...]
    internal_network: str = ""
    egress_network: str = ""


def git_gate_upstreams_for_bottle(bottle: Bottle) -> tuple[GitGateUpstream, ...]:
    """Lift each `bottle.git` entry into a GitGateUpstream. Cross-entry
    validation (unique Names, no shadow route with bottle.ssh) already
    ran in `manifest.Bottle.from_dict`."""
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
    """Posix-sh entrypoint (alpine ash). One `init_repo` call per
    upstream, then `exec git daemon`. The function reads
    `/git-gate/creds/<name>-{key,known_hosts}` (laid down by
    `DockerGitGate.start` via docker cp) and wires them into each
    bare repo's config so the shared pre-receive hook can pick them
    up at push time."""
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
        "  chmod 600 \"$keyfile\"",
        "  if [ -f \"$hostsfile\" ]; then",
        "    chmod 600 \"$hostsfile\"",
        "  fi",
        "",
        "  repo=/git/${name}.git",
        "  if [ ! -d \"$repo\" ]; then",
        "    git init --bare \"$repo\" >/dev/null",
        "  fi",
        "  git -C \"$repo\" config remote.upstream.url \"$upstream_url\"",
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
        "  --verbose",
    ])
    return "\n".join(lines) + "\n"


def git_gate_render_hook() -> str:
    """The shared pre-receive hook: gitleaks-scan all incoming refs,
    then forward each accepted ref to the real upstream using the
    per-repo credential. Failure in either phase aborts the push so
    the agent sees a real rejection. POSIX sh.

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

# Phase 2: forward each ref to the upstream.
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
  echo "git-gate: forwarding $ref to upstream" >&2
  if ! GIT_SSH_COMMAND="$ssh_cmd" git push upstream "$refspec" 1>&2; then
    echo "git-gate: upstream push failed for $ref" >&2
    exit 1
  fi
done < "$refs_file"

exit 0
"""


class GitGate(ABC):
    """The per-agent git-gate. Encapsulates the host-side prepare
    (upstream lift + entrypoint/hook render); the sidecar's
    start/stop lifecycle is backend-specific and lives on concrete
    subclasses."""

    def prepare(self, bottle: Bottle, slug: str, stage_dir: Path) -> GitGatePlan:
        """Compute the upstream table from `bottle.git` and write the
        entrypoint + pre-receive scripts (mode 600) under `stage_dir`.
        Pure host-side, no docker subprocess.

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
        return GitGatePlan(
            slug=slug,
            entrypoint_script=entrypoint,
            hook_script=hook,
            upstreams=upstreams,
        )

    @abstractmethod
    def start(self, plan: GitGatePlan) -> str:
        """Bring up the gate sidecar according to `plan`. Returns the
        target string identifying the running instance — the same
        value to pass to `.stop`. Backend-specific."""

    @abstractmethod
    def stop(self, target: str) -> None:
        """Tear down the gate sidecar identified by `target` (the
        value `.start` returned). Idempotent: a missing target is
        success. Backend-specific."""
