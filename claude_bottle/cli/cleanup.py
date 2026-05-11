"""cleanup: stop and remove all orphaned claude-bottle resources
(containers + networks) left behind by previous bottles."""

from __future__ import annotations

import sys

from ..platform import get_bottle_platform
from ..log import info
from ._common import read_tty_line


def cmd_cleanup(_argv: list[str]) -> int:
    platform = get_bottle_platform()
    plan = platform.prepare_cleanup()

    if plan.empty:
        info("no claude-bottle resources to clean up")
        return 0

    plan.print()
    sys.stderr.write("claude-bottle: remove all of the above? [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    if reply not in ("y", "Y", "yes", "YES"):
        info("aborted")
        return 0

    platform.cleanup(plan)
    info("done")
    return 0
