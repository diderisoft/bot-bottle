"""smolmachines `_resolve_plan` (PRD 0023 chunk 2d).

Resolves the per-bottle docker subnet + bundle IP, pre-packs the
agent's `.smolmachine` artifact (cached under
`~/.cache/claude-bottle/smolmachines/`), and assembles the guest
env. No VM bringup — that's `launch.launch`'s job."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ...backend import BottleSpec
from ...backend.docker.bottle_state import (
    BottleMetadata,
    agent_state_dir,
    bottle_identity,
    write_metadata,
)
from . import smolvm as _smolvm
from .bottle_plan import SmolmachinesBottlePlan
from .util import smolmachines_bundle_subnet, smolmachines_preflight


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
    # Chunk 2d placeholder until the agent-image work lands.
    # alpine pulls cleanly from docker.io via smolvm's crane
    # backend; the real claude-bottle image lives in the local
    # docker daemon and isn't reachable that way.
    agent_image_ref = "alpine:latest"
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
    )


def _ensure_smolmachine(image_ref: str) -> Path:
    """Cache `smolvm pack create --image <image_ref>` output under
    `~/.cache/claude-bottle/smolmachines/<slug>`. Returns the
    `.smolmachine.smolmachine` sidecar path — that's the file
    `machine create --from` consumes (pack create produces a
    launcher binary at `.smolmachine` plus the sidecar alongside
    it; the sidecar is the actual artifact).

    Re-runs of pack create against the same image hit smolvm's
    layer cache; we still skip the call entirely when the
    sidecar is already on disk, since each invocation costs
    several seconds even on a hot cache."""
    _SMOLMACHINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = image_ref.replace(":", "_").replace("/", "_")
    binary = _SMOLMACHINE_CACHE_DIR / f"{slug}.smolmachine"
    sidecar = _SMOLMACHINE_CACHE_DIR / f"{slug}.smolmachine.smolmachine"
    if not sidecar.is_file():
        _smolvm.pack_create(image_ref, binary)
    return sidecar
