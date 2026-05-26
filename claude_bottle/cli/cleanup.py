"""cleanup: stop and remove all orphaned claude-bottle resources.

PRD 0018 chunk 4: backend's prepare_cleanup carries everything in
one plan — live compose projects (whose `compose down` removes
containers + networks atomically), legacy stray containers/networks
that aren't in any project, and orphan state dirs (per-bottle
state with no live project AND no `.preserve` marker). One prompt,
one cleanup call.

State dirs with `.preserve` are intentionally never touched — they
hold capability-block rebuilds or crash snapshots the operator may
want to `resume`. Manual `rm -rf ~/.claude-bottle/state/<identity>`
is the path for those.
"""

from __future__ import annotations

import sys

from ..backend import get_bottle_backend
from ..log import info
from ._common import read_tty_line


def cmd_cleanup(_argv: list[str]) -> int:
    backend = get_bottle_backend()
    plan = backend.prepare_cleanup()

    if plan.empty:
        info("no claude-bottle resources to clean up")
        return 0

    plan.print()
    if not _prompt_yes("remove all of the above?"):
        info("cleanup: skipped")
        return 0

    backend.cleanup(plan)
    info("cleanup: done")
    return 0


def _prompt_yes(message: str) -> bool:
    sys.stderr.write(f"claude-bottle: {message} [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")
