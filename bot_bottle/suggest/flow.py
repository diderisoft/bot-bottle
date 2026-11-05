"""The one bottle-suggestion flow (PRD 0117), shared by
`bot-bottle bottles suggest` and the console's launch page.

`bottle-architect` answers first; when it can't (no agent, a harness that
can't run with no tools, not logged in, a failure, an answer that isn't a
proposal), the rules path does, and the proposal says why. Either way the
proposal is validated against the bottles the project sees and carries
`source` (`architect` or `rules`). `rules=True` skips the architect: the
offline, deterministic path.

The architect runs in its own bottle, which the host controller keeps warm
(`control/hostctl/architect_bottle.py`): `run` is how to ask it. The CLI
asks the host controller for the whole suggestion (`POST /suggest`), so the
flow runs there for every caller.
"""

from __future__ import annotations

from pathlib import Path

from ..paths import bot_bottle_root
from .architect import ArchitectError, Runner, architect_suggest
from .proposal import Proposal
from .rules import rules_suggest
from .runnable import host_secrets
from .validate import validate_proposal

SOURCE_ARCHITECT = "architect"
SOURCE_RULES = "rules"

NO_RUNNER = "only the host controller runs it, in its bottle"

NO_PROJECT_NOTE = ("no project directory was given, so the project's bottle, origin "
                   "and lockfiles weren't considered")


def bottle_dirs(cwd: Path | None, host_root: Path) -> list[tuple[Path, bool]]:
    """The bottles a proposal is validated against: the host's, the project's
    (when there is one) and the built-in ones."""
    # pylint: disable=import-outside-toplevel
    from ..manifest import ManifestIndex
    from ..manifest.loader import BUILTIN_BOTTLES_DIR
    if cwd is None:
        return [(host_root / "bottles", False), (BUILTIN_BOTTLES_DIR, False)]
    index = ManifestIndex.resolve(str(cwd), missing_ok=True)
    return index.bottle_dirs() if index.home_md else [(BUILTIN_BOTTLES_DIR, False)]


def suggest(
    task: str,
    cwd: Path | None,
    *,
    rules: bool = False,
    run: Runner | None = None,
    host_root: Path | None = None,
) -> Proposal:
    """A validated proposal for `task`. `cwd` is the project directory, or
    None when there's none to look at. `run` asks the architect; without
    one, the rules path answers and says why."""
    host_root = host_root or bot_bottle_root()
    notes = [] if cwd is not None else [NO_PROJECT_NOTE]
    if rules:
        proposal, source = rules_suggest(task, cwd, host_root=host_root), SOURCE_RULES
    else:
        try:
            if run is None:
                raise ArchitectError(NO_RUNNER)
            proposal = architect_suggest(task, cwd, run=run, host_root=host_root)
            source = SOURCE_ARCHITECT
        except ArchitectError as e:
            proposal, source = rules_suggest(task, cwd, host_root=host_root), SOURCE_RULES
            notes.append(f"bottle-architect didn't answer ({e}); this is the rules path")
    proposal.notes[:0] = notes
    validated = validate_proposal(proposal, bottle_dirs(cwd, host_root),
                                  host_secrets(path=host_root / "secrets"))[0]
    validated.source = source
    return validated


__all__ = ["NO_PROJECT_NOTE", "NO_RUNNER", "SOURCE_ARCHITECT", "SOURCE_RULES", "bottle_dirs",
           "suggest"]
