"""`GitGatePlan` — the git-gate launch plan (DTO).

The output of `GitGate.prepare`, consumed by the backend launch step and the
`BottlePlan` contract. A pure value type (its `upstreams` element type
`GitGateUpstream` is the neutral render dataclass).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..gateway.git_gate.render import GitGateUpstream


@dataclass(frozen=True)
class GitGatePlan:
    """Output of GitGate.prepare; consumed by .start.

    The script + slug + upstream fields are filled at prepare time (host-side,
    side-effect-free on docker). The network fields are populated by the
    backend's launch step via `dataclasses.replace` once those networks exist.
    Empty defaults are sentinels meaning "not yet set"; `.start` validates that
    they are populated.

    `hook_script` is the shared `pre-receive` for push-time gating;
    `access_hook_script` is `git daemon`'s `--access-hook` for the fetch-time
    upstream refresh. `gitleaks_config` is the rendered TOML that the hook
    passes to `gitleaks --config`; `Path()` is the sentinel "not yet rendered"
    value for callers that construct the plan directly in tests."""

    slug: str
    entrypoint_script: Path
    hook_script: Path
    access_hook_script: Path
    upstreams: tuple[GitGateUpstream, ...]
    gitleaks_config: Path = Path()
    internal_network: str = ""
    egress_network: str = ""
