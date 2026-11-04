"""Backend-neutral workspace porting.

The workspace is copied filtered: in a git repo, tracked plus untracked
non-ignored files and `.git`; elsewhere, everything but `SKIP_DIRS`. At exit,
`export_patch_script` turns everything changed since the copy (commits
included) into a patch.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .git_config_filter import filtered_config, is_repo_config


WORKSPACE_DIRNAME = "workspace"
DEFAULT_WORKSPACE_OWNER = "node:node"
DEFAULT_WORKSPACE_MODE = "755"


class WorkspaceSpec(Protocol):
    @property
    def copy_cwd(self) -> bool:
        ...

    @property
    def user_cwd(self) -> str:
        ...


@dataclass(frozen=True)
class WorkspacePlan:
    """Resolved workspace contract shared by all backends."""

    enabled: bool
    host_path: Path
    guest_home: str
    guest_path: str
    workdir: str
    owner: str = DEFAULT_WORKSPACE_OWNER
    mode: str = DEFAULT_WORKSPACE_MODE
    copy_contents: bool = True
    has_host_git_dir: bool = False


@dataclass(frozen=True)
class DiskInfo:
    """A backend's kept per-project workspace disk, for `cleanup workspaces`."""

    path: Path
    project: str
    # Allocated, so a sparse disk counts what it holds.
    bytes: int
    in_use: bool
    # Its project directory is gone.
    orphaned: bool


def workspace_plan(spec: WorkspaceSpec, *, guest_home: str) -> WorkspacePlan:
    """Resolve the in-bottle workspace from CLI intent."""
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
    """`~/.bot-bottle/work/<id>.patch`."""
    from .paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
    return bot_bottle_root() / "work" / f"{identity}.patch"


def save_patch(identity: str, patch: str) -> Path | None:
    """Save a non-empty exit patch (0600); otherwise clear a stale one."""
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


# Skipped outside a git repo (a repo uses its .gitignore).
SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build", ".next",
    ".mypy_cache", ".pytest_cache", ".tox",
})
LARGE_WORKSPACE_BYTES = 1 << 30
# In the guest: the commit the workspace was copied at.
BASE_FILE = ".git/bot-bottle-base"


def _git_listed(root: Path) -> list[str] | None:
    """Tracked plus untracked non-ignored files, or None if git can't list
    them."""
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
    """Paths to copy, relative to `root`."""
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


def pack_workspace(root: Path, dest: Path, *, is_git: bool,
                   dropped: list[str] | None = None) -> int:
    """Write the filtered workspace to tar `dest`; returns bytes packed. Files
    that vanish meanwhile are skipped. A repo config goes in cut to local
    settings (`git_config_filter`); the credential settings it left out are
    appended to `dropped`, by name."""
    total = 0
    with tarfile.open(dest, "w") as tar:
        for rel in workspace_files(root, is_git=is_git):
            path = root / rel
            try:
                info = tar.gettarinfo(str(path), arcname=rel)
            except OSError:
                continue
            if is_git and info.isreg() and is_repo_config(rel):
                data, left_out = filtered_config(path)
                if dropped is not None:
                    dropped.extend(f"{rel}: {name}" if rel != ".git/config" else name
                                   for name in left_out)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                total += info.size
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
    """Guest shell: record the commit the workspace was copied at."""
    return f"cd {guest_path} && git rev-parse HEAD > {BASE_FILE} 2>/dev/null || true"


def resync_clear_script(guest_path: str, *, is_git: bool) -> str:
    """Guest shell, before a copy onto a kept workspace disk: remove every
    file the copy would bring (in a repo, what git lists, plus `.git`;
    elsewhere, everything outside `SKIP_DIRS`), keeping what the project
    ignores. Fails, for a fresh start, when git can't list the files."""
    path = shlex.quote(guest_path)
    if is_git:
        return (
            f"cd {path} && files=$(mktemp) && "
            f"GIT_DIR=.git GIT_WORK_TREE=. "
            f"git -c core.fsmonitor=false ls-files -z --cached --others --exclude-standard "
            f"> \"$files\" && xargs -0 -r rm -f -- < \"$files\"; rc=$?; rm -f \"$files\"; "
            f"[ $rc -eq 0 ] && rm -rf .git"
        )
    prune = " -o ".join(f"-name {shlex.quote(d)}" for d in sorted(SKIP_DIRS))
    return (f"cd {path} && find . -mindepth 1 -type d \\( {prune} \\) -prune "
            f"-o ! -type d -exec rm -f -- {{}} +")


def export_patch_script(guest_path: str) -> str:
    """Guest shell printing a binary-safe patch of everything changed since the
    copy, commits included; empty if nothing changed."""
    return (
        f"cd {guest_path} 2>/dev/null && [ -f {BASE_FILE} ] && "
        f"git add -A >/dev/null 2>&1 && git diff --cached --binary \"$(cat {BASE_FILE})\""
    )
