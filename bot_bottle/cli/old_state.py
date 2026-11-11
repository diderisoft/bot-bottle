"""What versions up to v0.1.9 kept under a state root that nothing reads now
(ADR 0020): `bot-bottle uninstall` removes it, and install.md's "Upgrading to
this release" lists it for anyone cleaning up by hand. Nothing else reads this
list, and nothing reads the files.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Globs relative to a state root (`~/.bot-bottle`, `~/.bot-bottle-envs/<name>`).
OLD_STATE = (
    "console.json", "remote.json", "remote.status.json",
    "trusted-bottles.json", "audit-passkeys.json", "audit-push",
    "start.json", "github-app.json", "forges.md", "env.json",
    "docker-gateway-subnet", "docker-ports",
    "hostctl/state.json", "hostctl/sessions", "hostctl/sessions.sock",
    "infra/orchestrator.json", "infra/*.settings.json",
    "state/quickstart.json", "state/isolation-verified.json",
    "state/*/metadata.json", "state/*/adoption.json",
    "state/*/manifest/log.json", "state/*/manifest/launch.json",
    "transcripts/*/*/*.meta.json", "transcripts/*/*/follow.json",
    "snapshots/*/*/*.json",
    "workspace-disks/*.json",
)


def old_files(root: Path) -> list[Path]:
    """What of `OLD_STATE` is under `root`, sorted."""
    found: set[Path] = set()
    for pattern in OLD_STATE:
        found.update(p for p in root.glob(pattern) if p.exists() or p.is_symlink())
    return sorted(found)


def remove_old_files(root: Path) -> list[Path]:
    """Delete what `old_files` finds under `root`; what was deleted."""
    removed: list[Path] = []
    for path in old_files(root):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


__all__ = ["OLD_STATE", "old_files", "remove_old_files"]
