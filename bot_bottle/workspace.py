"""Backend-neutral plan for porting the operator workspace.

The workspace is copied in filtered (PRD 0094): for a git repo, what git
tracks plus untracked files it doesn't ignore, plus `.git`; elsewhere,
everything but a short list of dependency and build directories. A full copy
took 180 s on a 3.2 GB repo where the filtered one takes about 4 s. When the
agent exits, `EXPORT_PATCH_SCRIPT` turns everything it changed since the copy
(commits included) into a patch, so the work outlives the bottle.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


WORKSPACE_DIRNAME = "workspace"
DEFAULT_WORKSPACE_OWNER = "node:node"
DEFAULT_WORKSPACE_MODE = "755"


class WorkspaceSpec(Protocol):
    @property
    def copy_cwd(self) -> bool:
        """Whether to copy the current working directory."""
        ...

    @property
    def user_cwd(self) -> str:
        """The user's current working directory."""
        ...


@dataclass(frozen=True)
class WorkspacePlan:
    """Resolved workspace contract shared by all bottle backends."""

    enabled: bool
    host_path: Path
    guest_home: str
    guest_path: str
    workdir: str
    owner: str = DEFAULT_WORKSPACE_OWNER
    mode: str = DEFAULT_WORKSPACE_MODE
    copy_contents: bool = True
    copy_git: bool = True
    has_host_git_dir: bool = False


def workspace_plan(spec: WorkspaceSpec, *, guest_home: str) -> WorkspacePlan:
    """Resolve the in-bottle workspace path from CLI intent."""
    host_path = Path(spec.user_cwd).expanduser()
    if spec.copy_cwd:
        guest_path = f"{guest_home.rstrip('/')}/{WORKSPACE_DIRNAME}"
        workdir = guest_path
    else:
        guest_path = guest_home
        workdir = guest_home
    return WorkspacePlan(
        enabled=spec.copy_cwd,
        host_path=host_path,
        guest_home=guest_home,
        guest_path=guest_path,
        workdir=workdir,
        has_host_git_dir=(host_path / ".git").is_dir(),
    )


def work_patch_path(identity: str) -> Path:
    """Where a bottle's exit patch is saved: `~/.bot-bottle/work/<id>.patch`."""
    from .paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
    return bot_bottle_root() / "work" / f"{identity}.patch"


def save_patch(identity: str, patch: str) -> Path | None:
    """Save a non-empty exit patch (mode 0600); clear a stale one otherwise."""
    path = work_patch_path(identity)
    if not patch.strip():
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(patch)
    return path


def patch_summary(patch: str) -> tuple[int, int, int]:
    """(files, lines added, lines removed) in a git patch."""
    files = added = removed = 0
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++ "):
            added += 1
        elif line.startswith("-") and not line.startswith("--- "):
            removed += 1
    return files, added, removed


# Skipped when the workspace isn't a git repo (a repo uses its .gitignore).
SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", ".next",
    ".mypy_cache", ".pytest_cache", ".tox",
})
LARGE_WORKSPACE_BYTES = 1 << 30
# In the guest: the commit the workspace was copied at, for the exit patch.
BASE_FILE = ".git/bot-bottle-base"


def _git_listed(root: Path) -> list[str] | None:
    """What git tracks plus untracked files it doesn't ignore, or None when
    git can't list them (no git, or a broken repo): then nothing is filtered
    by .gitignore."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [f for f in out.decode("utf-8", "surrogateescape").split("\0") if f]


def workspace_files(root: Path, *, is_git: bool) -> list[str]:
    """The paths to copy, relative to ``root``."""
    listed = _git_listed(root) if is_git else None
    if listed is not None:
        files = listed
        for dirpath, _dirs, names in os.walk(root / ".git"):
            rel = Path(dirpath).relative_to(root)
            files.extend(str(rel / n) for n in names)
        return files
    files = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(root)
        files.extend(str(rel / n) if str(rel) != "." else n for n in names)
    return files


def pack_workspace(root: Path, dest: Path, *, is_git: bool) -> int:
    """Write the filtered workspace to the tar file ``dest``. Returns the
    bytes packed. Files that vanish while packing are skipped."""
    total = 0
    with tarfile.open(dest, "w") as tar:
        for rel in workspace_files(root, is_git=is_git):
            path = root / rel
            try:
                info = tar.gettarinfo(str(path), arcname=rel)
            except OSError:
                continue
            if info.isreg():
                try:
                    with path.open("rb") as f:
                        tar.addfile(info, f)
                except OSError:
                    continue
                total += info.size
            elif info.issym() or info.isdir():
                tar.addfile(info)
    return total


def record_base_script(guest_path: str) -> str:
    """Guest shell: remember the commit the workspace was copied at."""
    return f"cd {guest_path} && git rev-parse HEAD > {BASE_FILE} 2>/dev/null || true"


def export_patch_script(guest_path: str) -> str:
    """Guest shell printing a binary-safe patch of everything changed since
    the copy, commits included, or nothing when nothing changed. Staging is
    harmless: the bottle is about to go."""
    return (
        f"cd {guest_path} 2>/dev/null && [ -f {BASE_FILE} ] && "
        f"git add -A >/dev/null 2>&1 && git diff --cached --binary \"$(cat {BASE_FILE})\""
    )
