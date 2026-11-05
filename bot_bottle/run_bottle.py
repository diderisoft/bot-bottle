"""A run's unsaved bottle (ADR 0017): a suggested bottle used for one run only.

`bot-bottle start` runs a suggestion without saving it unless asked. The
launch request carries the bottle's text (`LaunchRequest.run_bottle`, `cli`
role only), named `RUN_BOTTLE` in its bottle list. The host controller
stages it where only that launch's manifest index looks
(`ManifestIndex.extra_bottle_dirs`), then keeps it in the bottle's state dir
(`<state>/bottles/`), so a resume finds it and the console can show it. It is
never added to the host's library: no listing, picker or suggestion sees it,
the console can't edit it, and "keep" never writes into it. Its full text is
in the run's audit (`lifecycle.bottled_agent_start`'s `bottle_versions`),
and the run's resolved manifest records what it resolved to.
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bottle_state import bottle_state_dir
from .contract.hostctl import RUN_BOTTLE, RUN_BOTTLE_MAX_BYTES

if TYPE_CHECKING:
    from .manifest import ManifestIndex

# Its name in a launch's bottle list (a host bottle of the same name is
# shadowed for that run only) and its size cap: `contract.hostctl`.
MAX_BYTES = RUN_BOTTLE_MAX_BYTES
_STAGED = "run-bottle"


def state_dir(identity: str) -> Path:
    """Where a run keeps its unsaved bottle."""
    return bottle_state_dir(identity) / "bottles"


def is_run_path(path: Path) -> bool:
    """Whether `path` is a run's unsaved bottle, not a library one: staged
    for a launch, or kept in a run's state dir."""
    if path.name != f"{RUN_BOTTLE}.md":
        return False
    return path.parent.name == _STAGED or path.parent == state_dir(path.parent.parent.name)


def index_for(index: "ManifestIndex", *, text: str = "", identity: str = "") -> "ManifestIndex":
    """`index` that also sees the run's bottle: `text` staged for a new
    launch, or the one a run kept (`identity`, for a resume or a view)."""
    if text:
        staged = Path(tempfile.mkdtemp(prefix="bot-bottle-run.")) / _STAGED
        staged.mkdir()
        (staged / f"{RUN_BOTTLE}.md").write_text(text, encoding="utf-8")
        directory = staged
    elif identity and (state_dir(identity) / f"{RUN_BOTTLE}.md").is_file():
        directory = state_dir(identity)
    else:
        return index
    return dataclasses.replace(index, extra_bottle_dirs=(directory, *index.extra_bottle_dirs))


def keep(identity: str, index: "ManifestIndex") -> None:
    """Copy a launch's staged bottle into the run's state dir. The staged copy
    stays (the launch may read it again); it's a small file in the temp dir."""
    for directory in index.extra_bottle_dirs:
        staged = directory / f"{RUN_BOTTLE}.md"
        if directory.name == _STAGED and staged.is_file() and identity:
            target = state_dir(identity)
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, target / staged.name)


def with_content(versions: dict[str, Any], text: str) -> dict[str, Any]:
    """The launch's bottle versions for its audit, with the unsaved bottle's
    full text when it has one."""
    return {**versions, "run_bottle": text} if text else versions


__all__ = ["MAX_BYTES", "RUN_BOTTLE", "index_for", "is_run_path", "keep", "state_dir",
           "with_content"]
