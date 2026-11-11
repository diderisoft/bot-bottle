"""`bot-bottle host`: settings for this host as a whole.

  budget [--memory GB] [--disk GB]   show or set the bottles' budget: memory
                                     allocated to running bottles, and storage
                                     in bot-bottle's own directories (0 clears
                                     one). A launch that wouldn't fit is refused.
  caps                               the old name for `budget` (deprecated)
  restore-db [BACKUP] [--list] [--yes]
                                     put a backup of the host database back,
                                     the newest or the one named, while the
                                     host controller is stopped. Asks first:
                                     trust records, passkeys and forge
                                     connections return as the backup has
                                     them

The host controller reads the budget at each launch and each time it reports
to the console, so a change applies at once, no restart needed. The budget
is in the host DB, so it's read and set through the host controller.

The host controller checks the host DB when it starts and keeps a daily
backup. It refuses to start on a DB that fails the check, naming the newest
backup; `restore-db` puts it back.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ...backend.bottle_size import BOTTLE_MEMORY_MIB
from ...control.hostctl import runner
from ...control.hostctl.client import HostControlRefused
from ...control.hostctl.runner import HostControlError
from ...lib.config_store import HostBudget, HostConfig
from .. import host_state_client as host
from ..constants import PROG


_RUNNING = f"the host controller is running; stop it first (`{PROG} down`), then restore"


def _gb(value: int) -> str:
    return f"{value} GB" if value else "none"


def _budget(ns: argparse.Namespace) -> int:
    if ns.action == "caps":
        sys.stderr.write(f"{PROG} host caps is now `{PROG} host budget`\n")
    if any(v is not None and v < 0 for v in (ns.disk, ns.memory)):
        sys.stderr.write(f"{PROG} host budget: GB must be 0 or more (0 clears it)\n")
        return 2
    shown = host.host_config().host_budget
    if ns.disk is not None or ns.memory is not None:
        shown = host.set_host_config(HostConfig(host_budget=HostBudget(
            disk_gb=shown.disk_gb if ns.disk is None else ns.disk,
            memory_gb=shown.memory_gb if ns.memory is None else ns.memory)),
            "host_budget").host_budget
    sys.stdout.write(f"memory: {_gb(shown.memory_gb)} "
                     f"(each bottle is given {BOTTLE_MEMORY_MIB // 1024} GB unless its resources: say otherwise)\n"
                     f"storage: {_gb(shown.disk_gb)}\n")
    return 0


def _restore_db(ns: argparse.Namespace) -> int:
    """Replace the host DB with a backup. The host controller is its only
    writer, so it must be stopped, and stays stopped while this runs."""
    # pylint: disable-next=import-outside-toplevel
    from ...control.hostctl import host_db
    if ns.list:
        found = host_db.backups()
        if not found:
            sys.stdout.write(f"no backups in {host_db.backups_dir()}\n")
        for backup in found:
            sys.stdout.write(f"{backup.path.name}  taken {backup.taken:%Y-%m-%d %H:%M} UTC\n")
        return 0
    chosen = None
    if ns.backup:
        chosen = Path(ns.backup)
        if chosen.name == ns.backup:  # a name from --list
            chosen = host_db.backups_dir() / ns.backup
    if chosen is None and not host_db.backups():
        sys.stderr.write(f"{PROG} host restore-db: no backups in {host_db.backups_dir()}\n")
        return 1
    if not _controller_stopped() or not _confirm_restore(chosen, ns.yes):
        return 1
    # The host controller's lifetime lock: none runs, and none starts while
    # the file is replaced.
    try:
        with runner.lock(), runner.daemon_lock():
            restored = host_db.restore(chosen)
    except runner.DaemonLockHeld:
        sys.stderr.write(f"{PROG} host restore-db: {_RUNNING}\n")
        return 1
    except (host_db.HostDbError, OSError) as e:
        sys.stderr.write(f"{PROG} host restore-db: {e}\n")
        return 1
    sys.stdout.write(f"restored {restored.backup.name} as {restored.db}\n")
    if restored.replaced is not None:
        sys.stdout.write(f"the database it replaced is kept at {restored.replaced}\n")
    return 0


def _controller_stopped() -> bool:
    """Whether no host controller runs (it holds its lifetime lock); says so
    when one does."""
    try:
        with runner.daemon_lock():
            return True
    except runner.DaemonLockHeld:
        sys.stderr.write(f"{PROG} host restore-db: {_RUNNING}\n")
        return False


def _confirm_restore(chosen: Path | None, yes: bool) -> bool:
    """Say what a restore brings back, and ask, unless `yes`."""
    # pylint: disable-next=import-outside-toplevel
    from ...control.hostctl import host_db
    newest = next(iter(host_db.backups()), None)
    which = (f"the backup taken {newest.taken:%Y-%m-%d %H:%M} UTC"
             if chosen is None and newest is not None else f"{chosen}")
    sys.stderr.write(
        f"{PROG} host restore-db: this puts the host database back as {which} has it. "
        "Trust records, audit passkeys and forge connections come back as they were then, "
        "including any removed since. Anything added or changed since is lost.\n")
    if yes:
        return True
    if not sys.stdin.isatty():
        sys.stderr.write(f"{PROG} host restore-db: not restoring without a terminal to "
                         "confirm; pass --yes\n")
        return False
    if input("restore it? [y/N] ").strip().lower() in ("y", "yes"):
        return True
    sys.stderr.write("left as it was\n")
    return False


def _add_budget(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", name: str,
                help_text: str) -> None:
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--memory", type=int, metavar="GB",
                        help="memory running bottles may be given, in GB (0 clears it)")
    parser.add_argument("--disk", type=int, metavar="GB",
                        help="storage bot-bottle's directories may use, in GB (0 clears it)")
    parser.set_defaults(run=_budget)


def cmd_host(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} host", description=(__doc__ or "").split("\n", maxsplit=1)[0])
    sub = parser.add_subparsers(dest="action", required=True)
    _add_budget(sub, "budget", "show or set the bottles' memory and storage budget")
    _add_budget(sub, "caps", "deprecated: use `budget`")
    restore = sub.add_parser(
        "restore-db", help="put a backup of the host database back (the host controller "
                           "must be stopped)")
    restore.add_argument("backup", nargs="?",
                         help="a backup's name (from --list) or path; default: the newest")
    restore.add_argument("--list", action="store_true", help="list the backups, newest first")
    restore.add_argument("--yes", "-y", action="store_true",
                         help="don't ask before restoring (for scripts)")
    restore.set_defaults(run=_restore_db)
    ns = parser.parse_args(argv)
    try:
        return ns.run(ns)
    except HostControlRefused as e:
        sys.stderr.write(f"{PROG} host {ns.action}: {e.error}\n")
    except HostControlError as e:
        sys.stderr.write(f"{PROG} host {ns.action}: {e}\n")
    return 1


__all__ = ["cmd_host"]
