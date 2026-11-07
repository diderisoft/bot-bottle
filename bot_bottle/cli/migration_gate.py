"""The host DB's schema-migration gate: a command that opens the stores runs
only on a current schema. A new DB is created without asking; an old one is
migrated once the user agrees, or straight away for `assume_yes` (scripts and
services)."""

from __future__ import annotations

import sys

from ..lib.log import error
from ..orchestrator.store.store_manager import StoreManager
from ..paths import host_db_path


def host_db_current(*, assume_yes: bool = False, hint: str = "") -> bool:
    """Whether the host DB's schema is current, migrating it first if it's new
    or the user agrees. False (and why) when it was left out of date."""
    mgr = StoreManager(host_db_path())
    fresh = mgr.is_fresh()  # before is_migrated, which creates the file
    if mgr.is_migrated():
        return True
    if fresh:
        mgr.migrate()  # a new install: nothing to migrate from, so don't ask
        return True
    if assume_yes:
        sys.stderr.write("bot-bottle: database schema is out of date; migrating\n")
    else:
        sys.stderr.write("bot-bottle: database schema is out of date\n")
        sys.stderr.write("Migrate now? [y/N] ")
        sys.stderr.flush()
        try:
            answer = sys.stdin.readline().strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            error("migration required — re-run and confirm to migrate" + hint)
            return False
    mgr.migrate()
    return True


__all__ = ["host_db_current"]
