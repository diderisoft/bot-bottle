"""`GitGatePlan`: the git-gate launch plan, from `GitGate.prepare`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..control.git_gate_render import GitGateUpstream


@dataclass(frozen=True)
class GitGatePlan:
    """Output of `GitGate.prepare`. `hook_script` is the shared pre-receive,
    `access_hook_script` the fetch-time refresh, `gitleaks_config` the rendered
    TOML. Empty values mean not yet set."""

    slug: str
    hook_script: Path
    access_hook_script: Path
    upstreams: tuple[GitGateUpstream, ...]
    gitleaks_config: Path = Path()
    # The `Bot-Bottle` trailer every commit carries (`<bottle>/<run>`), how
    # pre-receive checks it (`require`, `warn` or `off`), and the file of
    # commits the copied workspace had that its remotes didn't (one SHA a
    # line, maybe none), which are exempt. `GitGate.prepare` sets all three;
    # git-gate refuses pushes to a repo provisioned without them.
    trailer: str = ""
    trailer_mode: str = ""
    baseline_file: Path = Path()
