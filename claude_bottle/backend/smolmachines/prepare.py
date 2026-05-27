"""smolmachines `_resolve_plan` (PRD 0023 chunks 2d + 4c).

Resolves the per-bottle docker subnet + bundle IP, builds the
agent's docker image from the repo Dockerfile, converts it into a
`.smolmachine` artifact via an ephemeral local registry (smolvm's
crane backend only reads registry refs), and assembles the guest
env. The `.smolmachine` is cached under
`~/.cache/claude-bottle/smolmachines/` keyed by the docker image
ID so Dockerfile changes invalidate the cache automatically.

No VM bringup — that's `launch.launch`'s job."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from ...backend import BottleSpec
from ...backend.docker import util as docker_mod
from ...backend.docker.bottle_state import (
    BottleMetadata,
    agent_state_dir,
    bottle_identity,
    egress_state_dir,
    git_gate_state_dir,
    pipelock_state_dir,
    supervise_state_dir,
    write_metadata,
)
from ...egress import Egress
from ...git_gate import GitGate
from ...pipelock import PipelockProxy
from ...supervise import Supervise
from . import smolvm as _smolvm
from .bottle_plan import SmolmachinesBottlePlan
from .local_registry import crane_push_tarball, ephemeral_registry
from .util import smolmachines_bundle_subnet, smolmachines_preflight


# Repo root, used as the `docker build` context for the agent image.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


# Per-host cache for `smolvm pack create` outputs. Keyed by the
# image ref so re-prepares for the same image hit the cache
# (pack create is idempotent on the smolvm side but takes several
# seconds even when no layer is fetched).
_SMOLMACHINE_CACHE_DIR = Path.home() / ".cache" / "claude-bottle" / "smolmachines"


# Gateway ports the bundle exposes inside its container — pipelock
# HTTPS proxy, git-gate's git-daemon, supervise's MCP. The agent
# inside the smolvm guest dials these on the bundle's pinned IP.
_BUNDLE_PIPELOCK_PORT = 8888
_BUNDLE_GIT_GATE_PORT = 9418
_BUNDLE_SUPERVISE_PORT = 9100


def resolve_plan(
    spec: BottleSpec, *, stage_dir: Path
) -> SmolmachinesBottlePlan:
    """Materialize the smolmachines plan. The bundle's docker
    subnet + pinned IP are derived from the slug; the agent's
    `.smolmachine` artifact is built (or cache-hit) here so
    launch's `machine create --from` boots without a registry
    pull. Per-bottle guest env + the TSI allow_cidrs land on the
    plan for launch to pass straight through to
    `machine create` flags."""
    smolmachines_preflight()

    manifest = spec.manifest
    bottle = manifest.bottle_for(spec.agent_name)

    slug = spec.identity or bottle_identity(spec.agent_name)

    # Record minimal metadata so `cli.py resume` can recover the
    # slug. Same schema as the docker backend.
    write_metadata(BottleMetadata(
        identity=slug,
        agent_name=spec.agent_name,
        cwd=spec.user_cwd if spec.copy_cwd else "",
        copy_cwd=spec.copy_cwd,
        started_at=datetime.now(timezone.utc).isoformat(),
        # No compose project for smolmachines bottles; chunk 4
        # will give dashboard discovery a backend-specific path.
        compose_project="",
    ))

    subnet, gateway, bundle_ip = smolmachines_bundle_subnet(slug)

    # Agent's env. IP literals; no DNS resolution inside the guest
    # (TSI allowlist contains only `<bundle_ip>/32` — no resolver).
    guest_env: dict[str, str] = {
        **bottle.env,
        "HTTPS_PROXY": f"http://{bundle_ip}:{_BUNDLE_PIPELOCK_PORT}",
        "HTTP_PROXY":  f"http://{bundle_ip}:{_BUNDLE_PIPELOCK_PORT}",
        "NO_PROXY":    "localhost,127.0.0.1",
    }
    if bottle.git:
        guest_env["GIT_GATE_URL"] = (
            f"git://{bundle_ip}:{_BUNDLE_GIT_GATE_PORT}"
        )
    if bottle.supervise:
        guest_env["MCP_SUPERVISE_URL"] = (
            f"http://{bundle_ip}:{_BUNDLE_SUPERVISE_PORT}"
        )

    # Inner Plans for the four bundle daemons. The ABCs are
    # platform-neutral — `.prepare()` writes config files + returns
    # a Plan dataclass with no backend-specific assumptions. State
    # dirs are still keyed by slug under the docker backend's
    # bottle_state layout (shared on-host convention; not a docker
    # dependency).
    pipelock_dir = pipelock_state_dir(slug)
    pipelock_dir.mkdir(parents=True, exist_ok=True)
    proxy_plan = PipelockProxy().prepare(bottle, slug, pipelock_dir)

    git_gate_dir = git_gate_state_dir(slug)
    git_gate_dir.mkdir(parents=True, exist_ok=True)
    git_gate_plan = GitGate().prepare(bottle, slug, git_gate_dir)

    egress_dir = egress_state_dir(slug)
    egress_dir.mkdir(parents=True, exist_ok=True)
    egress_plan = Egress().prepare(bottle, slug, egress_dir)

    supervise_plan = None
    if bottle.supervise:
        supervise_dir = supervise_state_dir(slug)
        supervise_dir.mkdir(parents=True, exist_ok=True)
        supervise_plan = Supervise().prepare(slug, supervise_dir)

    # Prompt file is always written (mode 0o600) so the in-VM
    # path always exists. Content is the agent's `prompt`
    # field (markdown body) — empty for agents with no prompt.
    # claude-code reads it via --append-system-prompt-file only
    # when non-empty, but the file must exist either way to
    # match the docker backend's contract.
    agent_dir = agent_state_dir(slug)
    agent_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = agent_dir / "prompt.txt"
    agent = manifest.agents[spec.agent_name]
    prompt_file.write_text(agent.prompt or "")
    prompt_file.chmod(0o600)

    machine_name = f"claude-bottle-{slug}"
    # Build the agent image from the repo Dockerfile (shared with
    # the docker backend, layer-cached) and convert it into a
    # `.smolmachine` artifact via an ephemeral local registry. The
    # CLAUDE_BOTTLE_IMAGE env var match the docker backend's
    # resolve_plan default so both backends use the same image when
    # one is built.
    agent_image_ref = os.environ.get(
        "CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest"
    )
    agent_from_path = _ensure_smolmachine(agent_image_ref)

    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        slug=slug,
        bundle_subnet=subnet,
        bundle_gateway=gateway,
        bundle_ip=bundle_ip,
        machine_name=machine_name,
        agent_from_path=agent_from_path,
        guest_env=guest_env,
        prompt_file=prompt_file,
        proxy_plan=proxy_plan,
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
    )


def _ensure_smolmachine(image_ref: str) -> Path:
    """Build the agent docker image and convert it into a
    `.smolmachine` artifact, caching the result under
    `~/.cache/claude-bottle/smolmachines/` keyed by the docker image
    ID (so a Dockerfile change automatically invalidates the cache).

    Returns the `.smolmachine.smolmachine` sidecar path — that's
    the file `machine create --from` consumes (pack create produces
    a launcher binary at `.smolmachine` plus the sidecar alongside
    it; the sidecar is the actual artifact).

    Conversion path: `docker build` (the existing layer cache
    makes no-change rebuilds cheap) → `docker save` to a tarball
    → spin up an ephemeral registry on a private docker network →
    `crane push --insecure` from a one-shot container on the same
    network → `smolvm pack create --image localhost:<host port>/...`
    → tear down the registry + network. The crane push detour
    sidesteps the Docker-Desktop daemon's HTTPS preference for
    non-loopback registries — see the `local_registry` module
    docstring for the gory details.

    Each pack-create costs several seconds even on a hot cache,
    so we skip the whole pipeline when the cached sidecar is
    already on disk for this image ID."""
    _SMOLMACHINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    docker_mod.build_image(image_ref, _REPO_DIR)
    # `sha256:abcd...` -> `abcd...` first 16 chars: short enough to
    # keep filenames manageable, long enough to make collisions
    # astronomically unlikely.
    digest = docker_mod.image_id(image_ref).split(":", 1)[-1][:16]
    binary = _SMOLMACHINE_CACHE_DIR / f"{digest}.smolmachine"
    sidecar = _SMOLMACHINE_CACHE_DIR / f"{digest}.smolmachine.smolmachine"
    if sidecar.is_file():
        return sidecar
    tarball = _SMOLMACHINE_CACHE_DIR / f"{digest}.image.tar"
    docker_mod.save(image_ref, str(tarball))
    try:
        with ephemeral_registry() as handle:
            push_ref = f"{handle.push_endpoint}/claude-bottle:{digest}"
            pack_ref = f"{handle.pull_endpoint}/claude-bottle:{digest}"
            crane_push_tarball(handle, str(tarball), push_ref)
            _smolvm.pack_create(pack_ref, binary)
    finally:
        # Tarball is ~500MB-1GB for the agent image; reclaim once
        # the smolmachine artifact exists. The artifact itself is
        # the long-lived cache entry.
        tarball.unlink(missing_ok=True)
    return sidecar
