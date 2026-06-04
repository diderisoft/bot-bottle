"""Backend-neutral plan for porting the operator workspace."""

from __future__ import annotations

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
