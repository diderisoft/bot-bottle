"""capability_apply — host-side orchestrator for capability-block
remediation (PRD 0016).

On approval of a capability-block proposal, the dashboard calls
apply_capability_change(slug, new_dockerfile) which:

  1. Snapshots the agent's transcript dir to
     ~/.bot-bottle/state/<slug>/transcript/ (best-effort).
  2. Pushes the agent's working tree via `git push` (best-effort —
     no upstream / no commits / no git repo all skip with a log).
  3. Writes the new Dockerfile to
     ~/.bot-bottle/state/<slug>/Dockerfile (PRD 0016 Phase 1
     state). The next `cli.py start <agent>` picks it up.
  4. Force-removes the agent container + all sidecars + the
     per-bottle networks. Idempotent — missing resources are not
     errors.

Returns (before, after) Dockerfile contents so the dashboard can
record / render the diff. (capability-block has no audit log per
PRD 0013 — the per-bottle Dockerfile state is its own record.)

This is "fire-and-forget" from the agent's perspective: by the time
the dashboard writes the response file the supervise sidecar is
gone, so the agent's tool call connection drops without ever
receiving the response. The replacement agent (next manual
`cli.py start`) sees the new Dockerfile and starts from there.
v1 does not auto-relaunch — see PRD 0016's capability-block return
semantics open question.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ...agent_provider import get_provider
from ...log import info, warn
from .bottle_state import (
    mark_preserved,
    per_bottle_dockerfile,
    transcript_snapshot_dir,
    write_per_bottle_dockerfile,
)
from .sidecar_bundle import sidecar_bundle_container_name


# Agent home inside the container (per the repo Dockerfile's
# `USER node` + `WORKDIR /home/node`). Used to locate the transcript
# dir + the workspace dir for git push.
_AGENT_HOME_IN_CONTAINER = "/home/node"
_AGENT_TRANSCRIPT_IN_CONTAINER = f"{_AGENT_HOME_IN_CONTAINER}/.claude"
_AGENT_WORKSPACE_IN_CONTAINER = f"{_AGENT_HOME_IN_CONTAINER}/workspace"

# Per-bottle resource name patterns (mirroring prepare.py).
def _agent_container_name(slug: str) -> str:
    return f"bot-bottle-{slug}"


def _per_bottle_container_names(slug: str) -> list[str]:
    """All container names that belong to this bottle. Missing
    containers are silently skipped by the teardown helper, so it's
    fine to include names that don't exist for a given bottle."""
    return [
        _agent_container_name(slug),
        sidecar_bundle_container_name(slug),
    ]


def _per_bottle_network_names(slug: str) -> list[str]:
    return [
        f"bot-bottle-net-{slug}",
        f"bot-bottle-egress-{slug}",
    ]


class CapabilityApplyError(RuntimeError):
    """Raised when the apply fails in a way that should keep the
    proposal pending (so the operator can retry). Best-effort
    failures (transcript snapshot, git push) do not raise — they
    just log and proceed."""


# --- Public helpers --------------------------------------------------------


def fetch_current_dockerfile(slug: str) -> str:
    """Return the Dockerfile content the next `cli.py start <agent>`
    would use for this bottle. If a per-bottle override exists, that
    one; otherwise the repo's Dockerfile.

    Used by the operator-edit verb to show the current source of
    truth, and by apply_capability_change for the before-diff."""
    override = per_bottle_dockerfile(slug)
    if override is not None:
        return override
    repo_dockerfile = get_provider("claude").dockerfile
    if repo_dockerfile is not None and repo_dockerfile.is_file():
        return repo_dockerfile.read_text()
    raise CapabilityApplyError(
        f"no per-bottle Dockerfile for {slug} and no provider Dockerfile at "
        f"{repo_dockerfile}"
    )


def apply_capability_change(slug: str, new_dockerfile: str) -> tuple[str, str]:
    """End-to-end capability-block remediation. See module docstring
    for the sequence. Returns (before, after) Dockerfile content."""
    if not new_dockerfile.strip():
        raise CapabilityApplyError("proposed Dockerfile is empty")
    before = fetch_current_dockerfile(slug)

    snapshot_transcript(slug)
    _push_working_tree(slug)
    write_per_bottle_dockerfile(slug, new_dockerfile)
    # Set the preserve marker BEFORE teardown so cli.py's session-end
    # cleanup sees it and keeps the state dir intact for the
    # operator's `cli.py resume <identity>`. Without the marker the
    # state dir would be deleted as part of normal session end.
    mark_preserved(slug)
    _teardown_bottle(slug)

    return before, new_dockerfile


# --- Internals -------------------------------------------------------------



def snapshot_transcript(slug: str) -> None:
    """`docker cp` /home/node/.claude out of the agent container into
    ~/.bot-bottle/state/<slug>/transcript/. Best-effort: missing
    container, missing dir, or cp error all log a warning and return.
    The transcript is what `claude --resume` reads to pick up where
    the agent left off.

    Called from two places:
      - capability-apply, before tearing the bottle down.
      - cli.py's session-end path, before the launch context closes,
        so a crash or normal exit also leaves a transcript on disk
        (deleted along with the state dir on clean exit, kept on
        crash or capability-block per the preserve marker)."""
    container = _agent_container_name(slug)
    dest = transcript_snapshot_dir(slug)
    if dest.exists():
        # Remove any prior snapshot so the new one is a clean copy.
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["docker", "cp", f"{container}:{_AGENT_TRANSCRIPT_IN_CONTAINER}", str(dest)],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        warn(
            f"transcript snapshot skipped "
            f"({(r.stderr or '').strip() or 'no transcript dir in container?'})"
        )
        return
    info(f"transcript snapshotted to {dest}")


def _push_working_tree(slug: str) -> None:
    """`docker exec <agent> git push` from /home/node/workspace.
    Best-effort: not-a-git-repo, no upstream, nothing-to-push, no
    network all log a warning and return. The replacement bottle
    will pick up whatever's actually upstream."""
    container = _agent_container_name(slug)
    r = subprocess.run(
        [
            "docker", "exec", container, "sh", "-c",
            f"cd {_AGENT_WORKSPACE_IN_CONTAINER} && "
            f"git rev-parse --is-inside-work-tree >/dev/null 2>&1 && "
            f"git push origin HEAD 2>&1 || true",
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        warn(
            f"capability-apply: git push skipped "
            f"({(r.stderr or '').strip() or 'docker exec failed'})"
        )
        return
    output = (r.stdout or "").strip()
    if output:
        info(f"capability-apply: git push: {output}")
    else:
        info("capability-apply: git push ran (no output — likely not a git workspace)")


def _teardown_bottle(slug: str) -> None:
    """Force-remove all per-bottle docker resources. Idempotent —
    `docker rm -f` / `docker network rm` silently ignore missing
    names, so this can be called even mid-rebuild."""
    info(f"capability-apply: tearing down bottle {slug}")
    for name in _per_bottle_container_names(slug):
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    for net in _per_bottle_network_names(slug):
        subprocess.run(
            ["docker", "network", "rm", net],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )


__all__ = [
    "CapabilityApplyError",
    "apply_capability_change",
    "fetch_current_dockerfile",
    "snapshot_transcript",
]
