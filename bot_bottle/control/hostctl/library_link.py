"""The library (PRD 0131): this host's `~/.bot-bottle` repo synced with the
account's repo in the console's library app, while the console says so.

- `library_sync` (`enabled`), on each connect: sync only while enabled. (Not
  `library`, which is PRD 0109's request for the host's agents and bottles.)
- `library_moved` (`head`): the account's `main` moved; fetch and fast-forward.
- `library_conflict` (`files`): this host's branch didn't merge. It keeps its
  own version, and keeps pushing, until a fetched `main` contains the commit
  it had then (`library_conflict.py`, in the host DB); CLI launches and
  `bot-bottle status` say a conflict is pending meanwhile.
- `library_pushed` (`head`, from the host): this host pushed its branch.
- `library_held` (`files`: `path` and `error` each, from the host): files
  changed since this host last pushed that don't parse, so it commits but
  doesn't push (`library_held.py`, in the host DB); `files: []` once they
  parse and it pushed. Sent when that changes, and on each connect's first
  sync.
- `library_builtins` (`version`, `files`, from the host): on each connect,
  the installed release's built-ins, so the console can list them and say
  what an upgrade changed in them. Not sent from a source checkout.

The account's version is `bot-bottle.lock` in the library, which only the
console writes; launches don't read it. When it names the release this host
runs, the host proposes that release's changes to copies of built-ins
(`library_upstream.py`).

While the library syncs, hand edits are committed once quiet
(`library_watch.py`), and the repo's commits are authored as the label this
host was paired as (`versions.set_author`).

The host pushes its `main` to `hosts/<host id>` and fetches `main`; it never
merges, only fast-forwards (`library_git.py`). A failed sync is tried again
later (sooner on a new commit or `library_moved`), after a longer wait for a
refusal that won't pass by itself: a deleted account (403) or a push past the
library's size limits (413).

A console launch names the library commit it runs (`library_commit`): the
host fetches `main` if it lacks that commit, then resolves library bottles and
agents from a copy of that commit's `agents/` and `bottles/` (`tree_at`)
first, its own copy only for names the commit lacks. The repo's working tree
and `main` are untouched, and a host whose `main` diverged still launches the
console's version.
"""

from __future__ import annotations

import asyncio
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import run_bottle
from ...contract.hostctl import LaunchRequest
from ...lib import log
from ...manifest import parse_check
from ...manifest.account_version import packaged_files, release_key
from ...manifest.versions import VersionError, launch_versions, set_author
from ...paths import bot_bottle_root, host_db_path
from . import pairing, project_worktrees
from .builtins_cache import installed_release
from .checkouts import learn_launch
from .library_conflict import Conflict, LibraryConflictStore
from .library_git import (KEEP_TREES, PUSHED_REF, TREES_DIR, LibraryError, LibrarySync, Synced,
                          Token, console_token, head_stamp)
from .library_held import Held, LibraryHeldStore
from .library_upstream import Repo, account_version
from .library_watch import HandEditWatch

if TYPE_CHECKING:
    from ...manifest import ManifestIndex

# How long a CLI launch waits for a sync before it uses the local copy.
LAUNCH_SYNC_SECONDS = 2.0
# The wait before trying a failed sync again: doubling from the first to the
# second, or the last after a refusal that won't pass by itself.
RETRY_SECONDS = (30.0, 600.0, 3600.0)
# A path the library takes (its `check_path`): a conflict on any other can't
# be resolved in the console, only by changing it here. And how many of a
# `library_conflict`'s files are kept, of how many characters.
_LIBRARY_ITEM = re.compile(r"(agents|bottles)/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.md")
_CONFLICT_FILES = 50
_CONFLICT_PATH = 300
# What the console keeps of `library_builtins`.
MAX_BUILTINS = 32
MAX_BUILTIN_BYTES = 64 * 1024
_BUILTIN_PATH = re.compile(r"(agents|bottles)/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.md", re.ASCII)


class _Attempt:
    """A sync a launch waits on; later launches wait on the same one."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error = ""


_active: "LibraryLink | None" = None


def sync_for_launch(timeout: float = LAUNCH_SYNC_SECONDS) -> str:
    """Sync before a CLI launch, waiting at most `timeout`: "" when the
    library is current (or not synced on this host), else a one-line note: a
    conflict still pending after the sync, or that it used the local copy."""
    note = _launch_sync_note(timeout)
    conflict = pending_conflict()
    return "\n".join(n for n in (note, conflict_note(conflict) if conflict else "") if n)


def _launch_sync_note(timeout: float) -> str:
    link = _active
    if link is None or not link.enabled:
        return ""
    return link.sync_for_launch(timeout)


def _conflicts() -> LibraryConflictStore:
    return _active.conflicts() if _active is not None else LibraryConflictStore(host_db_path())


def pending_conflict() -> Conflict | None:
    """The conflict this host waits on, while it syncs or stays paired (a
    restarted host controller whose link hasn't connected yet still has
    it)."""
    try:
        on = (_active is not None and _active.enabled) or pairing.load_config().enabled
        return _conflicts().pending() if on else None
    except (OSError, sqlite3.Error):
        return None


def conflict_files(raw: object) -> list[str]:
    """A `library_conflict` message's files, as kept and printed: paths with
    no control characters, at most `_CONFLICT_FILES` of `_CONFLICT_PATH`
    characters. Anything else is dropped."""
    files = [f for f in raw if isinstance(f, str) and 0 < len(f) <= _CONFLICT_PATH
             and f.isprintable()] if isinstance(raw, list) else []
    return list(dict.fromkeys(files))[:_CONFLICT_FILES]


def on_host(files: tuple[str, ...]) -> list[str]:
    """The conflicted files the library doesn't take, so the console can't
    resolve: they're fixed on this host."""
    return [f for f in files if not _LIBRARY_ITEM.fullmatch(f)]


def conflict_note(conflict: Conflict) -> str:
    """The line a CLI launch shows while a conflict is pending."""
    host = socket.gethostname().split(".")[0] or "this host"
    what = (f"{parse_check.names(conflict.files)} {'has' if len(conflict.files) == 1 else 'have'}"
            if conflict.files else "this host's library changes have")
    here = on_host(conflict.files)
    where = ("waiting in the console" if not here else
             f"the console can't resolve ({parse_check.names(tuple(here))}: fix "
             f"{'it' if len(here) == 1 else 'them'} on this host)")
    return f"library: {what} a conflict {where}; launching {host}'s version"


def _held_store() -> LibraryHeldStore:
    return _active.held_store() if _active is not None else LibraryHeldStore(host_db_path())


def pending_held() -> Held | None:
    """What this host holds back from sync because it doesn't parse, while
    it syncs or stays paired."""
    try:
        on = (_active is not None and _active.enabled) or pairing.load_config().enabled
        return _held_store().held() if on else None
    except (OSError, sqlite3.Error):
        return None


def label() -> str:
    """What this host was paired as: its library commits' author. An older
    pairing kept none; it was the hostname then, unless `--label` said."""
    return pairing.load_pairing().label or socket.gethostname().split(".")[0] or "host"


def report() -> dict[str, object]:
    """The library here, for `bot-bottle status`: `state` "conflict" (with
    `files`, `on_host`: those to fix here, and `since`), "off", or "synced"
    or "syncing" (with `head` and the library's `main` as last fetched). While
    the last sync failed, also its `error` and `retry_in` (seconds). `held`
    (`files`, each `path` and `error`, and `since`) while files that don't
    parse are held back."""
    link = _active
    held = pending_held()
    failing: dict[str, object] = {"held": {
        "files": [f.to_json() for f in held.files], "since": held.since}} if held else {}
    if link is not None and link.enabled and link.error:
        failing.update(error=link.error, retry_in=max(0.0, link.retry_at - time.monotonic()))
    conflict = pending_conflict()
    if conflict is not None:
        return {"state": "conflict", "files": list(conflict.files),
                "on_host": on_host(conflict.files), "since": conflict.since, **failing}
    if link is None or not link.enabled:
        return {"state": "off", **failing}
    try:
        sync = link.syncer()
        head, main = sync.heads()
        if sync.upstream_notes:
            failing["upstream"] = list(sync.upstream_notes)
        failing.update(_behind(sync.repo(), main))
    except (pairing.RemoteError, OSError):
        head, main = "", ""
    return {"state": "synced" if head and head == main else "syncing",
            "head": head, "main": main, **failing}


def _behind(repo: Repo, main: str) -> dict[str, object]:
    """`behind`: the account's version, when this host runs an older
    release (it launches on its own built-ins meanwhile)."""
    installed = installed_release()
    version = account_version(repo, main) if main and installed else ""
    return {"behind": version} if version and release_key(installed) < release_key(
        version) else {}


def builtins_message() -> dict[str, object] | None:
    """`library_builtins` for the installed release, within what the console
    keeps (it drops a larger message whole); None for a source checkout or
    an unknown version."""
    version = installed_release()
    if not version:
        return None
    files = {rel: text for rel, text in packaged_files().items()
             if _BUILTIN_PATH.fullmatch(rel) and len(text.encode()) <= MAX_BUILTIN_BYTES}
    return {"type": "library_builtins", "version": version,
            "files": dict(sorted(files.items())[:MAX_BUILTINS])}


def _launch_sync() -> LibrarySync:
    """The active link's sync (one git job at a time), else one for this
    host's pairing, whose token is asked for only if a fetch is needed."""
    if _active is not None:
        return _active.syncer()
    return LibrarySync(host_id="")


def library_tree(request: LaunchRequest) -> Path | None:
    """A launch's library commit, copied (`LibrarySync.tree_at`), for
    `ManifestIndex.with_library`; None without one."""
    commit = request.library_commit
    return _launch_sync().tree_at(commit) if commit else None


def commit_tree(request: LaunchRequest) -> Path | None:
    """Where `tree_at` copied a launch's library commit (no git; for its
    launch record, once it started), or None."""
    commit = request.library_commit
    if not commit:
        return None
    try:
        root = _launch_sync().root
    except pairing.RemoteError:  # unpaired since: the same default root
        root = bot_bottle_root()
    return root / TREES_DIR / commit


def launch_index(request: LaunchRequest, cwd: str,
                 manifest_for: Callable[[str], "ManifestIndex"],
                 ) -> "tuple[dict[str, object], ManifestIndex]":
    """A launch's bottle versions, for its audit, and the manifest index it
    resolves from. Without a library commit, hand edits become a version
    first (PRD 0103). With one, library bottles and agents come from that
    commit, else this host's copy (a host whose first sync hasn't merged
    keeps its own), and it's the host version; project bottles and
    built-ins resolve as ever. A project's checkout is kept as used
    (`checkouts`); a resume in a console project launch's worktree makes it
    again if it's gone."""
    commit = request.library_commit
    tree = library_tree(request)
    checkout = project_worktrees.restore(cwd)
    versions = launch_versions(cwd, host=commit)
    if checkout is not None:  # the checkout, never its worktree
        learn_launch(checkout, project=True)
    elif "project" in versions:  # the console lists the checkouts launched from
        learn_launch(cwd)
    index = manifest_for(cwd)
    if tree is not None:
        index = index.with_library(tree)
    return run_bottle.with_content(versions, request.run_bottle), index


class LibraryLink:
    """The library's messages on one console link."""

    REQUESTS = frozenset({"library_sync", "library_moved", "library_conflict"})

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 audit: Callable[[], Any] = lambda: None,
                 make_sync: Callable[[], LibrarySync] | None = None,
                 root: Path | None = None,
                 conflicts: LibraryConflictStore | None = None,
                 held: LibraryHeldStore | None = None) -> None:
        self._send = send
        self._audit = audit
        self._make_sync = make_sync
        self._root = root
        self._sync: LibrarySync | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: "asyncio.Task[None] | None" = None
        self._again = False
        self._seen: float = 0.0
        self._launch: _Attempt | None = None
        self._launch_lock = threading.Lock()
        self._conflicts = conflicts
        self._held = held
        self._told_held = False  # this connection's console heard what's held
        self._watch: HandEditWatch | None = None
        self._authored = False
        self.enabled = False
        # The last sync's failure ("" once one succeeds), the wait before the
        # next try, and when that is (time.monotonic(); 0: none due).
        self.error = ""
        self._backoff = 0.0
        self.retry_at = 0.0

    def conflicts(self) -> LibraryConflictStore:
        if self._conflicts is None:
            self._conflicts = LibraryConflictStore(host_db_path())
        return self._conflicts

    def held_store(self) -> LibraryHeldStore:
        if self._held is None:
            self._held = LibraryHeldStore(host_db_path())
        return self._held

    @property
    def conflict(self) -> list[str]:
        """The files of the conflict pending on the console, if one is."""
        pending = self.conflicts().pending()
        return list(pending.files) if pending is not None else []

    def syncer(self) -> LibrarySync:
        if self._sync is None:
            if self._make_sync is not None:
                self._sync = self._make_sync()
            else:
                paired = pairing.load_pairing().require()
                self._sync = LibrarySync(host_id=paired.host_id, audit=self._audit,
                                         root=self._root or bot_bottle_root())
        return self._sync

    async def handle(self, msg: dict[str, Any]) -> None:
        global _active  # pylint: disable=global-statement
        kind = msg.get("type")
        if kind == "library_sync":
            self.enabled = msg.get("enabled") is True
            _active = self if self.enabled else (None if _active is self else _active)
            if self.enabled:
                await asyncio.to_thread(self._author, True)
                self.schedule()
            else:  # off: nothing to resolve (on again, the next push hears anew)
                await asyncio.to_thread(self._off)
        elif kind == "library_moved" and self.enabled:
            self.schedule()
        elif kind == "library_conflict":
            await asyncio.to_thread(self._record_conflict, conflict_files(msg.get("files")))

    async def connected(self) -> None:
        """A (re)connected console hears this host's built-ins."""
        message = await asyncio.to_thread(builtins_message)
        if message is not None:
            await self._send(message)

    def _off(self) -> None:
        self.conflicts().clear()
        self.held_store().clear()
        self._author(False)

    def _author(self, on: bool) -> None:
        """Author the repo's commits as this host while the library syncs,
        else as bot-bottle. Before the repo exists, `tick` tries again once
        it does; a failure is logged, and tried again on the next
        `library_sync`, not on every tick."""
        root = self._root or bot_bottle_root()
        self._authored = False
        if not (root / ".git").is_dir():
            return
        self._authored = on
        try:
            set_author(label() if on else "", root=root)
        except (VersionError, pairing.RemoteError, OSError, sqlite3.Error) as e:
            log.warn(f"library: couldn't set this host's name on its commits: {e}")

    def _record_conflict(self, files: list[str]) -> None:
        """Keep the conflict, and the commit this host had, which `main`
        contains once it's resolved. Not when the `main` it last fetched
        already contains it: the message is older than that fetch."""
        try:
            sync = self.syncer()
            tip, main = sync.branch_tip(), sync.heads()[1]
            if tip and main and sync.contains(main, tip):
                return
        except (pairing.RemoteError, OSError):
            tip = ""
        self.conflicts().report(files, tip)
        log.warn("library: this host's changes conflict with the library "
                 f"({', '.join(files) or 'files unknown'}); resolve it in the "
                 "console. This host keeps its own version meanwhile.")

    def close(self) -> None:
        """The link closed: nothing syncs through it any more (a launch
        meanwhile uses the local copy, as on an unpaired host)."""
        global _active  # pylint: disable=global-statement
        self.enabled = False
        if _active is self:
            _active = None

    async def tick(self) -> None:
        """On the link's watch loop: hand edits are committed once quiet, a
        new local commit is synced, and a failed sync tried again once its
        wait is over."""
        if not self.enabled:
            return
        await asyncio.to_thread(self._hand_edits)
        stamp = await asyncio.to_thread(head_stamp, self._root or bot_bottle_root())
        due = bool(self.retry_at) and time.monotonic() >= self.retry_at
        if stamp != self._seen or due:
            self._seen = stamp
            self.retry_at = 0.0
            self.schedule()

    def _hand_edits(self) -> None:
        if not self._authored:
            self._author(True)
        if self._watch is None:
            self._watch = HandEditWatch(self._root or bot_bottle_root())
        try:
            if self._watch.poll():
                log.info("library: committed hand edits")
        except VersionError as e:
            log.warn(f"library: couldn't commit hand edits: {e}")

    def schedule(self) -> None:
        """Sync soon; a burst of reasons is one sync (and one more after it)."""
        self._loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                synced = await asyncio.to_thread(self.sync_now)
            except (LibraryError, pairing.RemoteError, OSError):
                synced = Synced()  # logged, and tried again later
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warn(f"library: sync failed: {e}")  # a bug, say: the link carries on
                synced = Synced()
            if synced.stamp:
                self._seen = synced.stamp
            await self._announce(synced)
            if not self._again:
                return

    def sync_now(self) -> Synced:
        """One sync, from any thread. A failure is logged once (until it
        changes) and sets when the next try is due. A pending conflict clears
        once the fetched `main` contains the commit this host's branch had
        then, or this host's push replaced what its branch held (a proposal
        that conflicted, say)."""
        sync = self.syncer()
        try:
            synced = sync.sync()
        except (LibraryError, pairing.RemoteError, OSError) as e:
            self._failed(e)
            raise
        if self.error:
            log.info("library: synced again")
        self.error, self._backoff, self.retry_at = "", 0.0, 0.0
        try:
            pending = self.conflicts().pending()
            if pending is not None and (synced.replaced or (
                    bool(synced.main) and sync.contains(synced.main, pending.tip)
                    if pending.tip else synced.moved)):
                self.conflicts().clear()
                log.info("library: the conflict was resolved in the console")
        except (OSError, sqlite3.Error) as e:
            log.warn(f"library: couldn't check the pending conflict: {e}")
        try:
            self._settle_held(synced)
        except (OSError, sqlite3.Error) as e:
            log.warn(f"library: couldn't keep what's held back: {e}")
        return synced

    def _settle_held(self, synced: Synced) -> None:
        """Keep what the sync held back, and whether the console should hear
        of it: when that changed, or it hasn't heard on this connection."""
        if synced.held is None:
            return
        store = self.held_store()
        before = store.held()
        if synced.held:
            store.hold(synced.held)
            changed = before is None or before.files != synced.held
            if changed:
                log.warn("library: not synced, since "
                         + "; ".join(f"{f.path} doesn't parse ({f.error})" for f in synced.held))
        else:
            changed = store.clear()
            if changed:
                log.info("library: the held files parse now; synced")
        synced.tell_held = changed or not self._told_held

    def _failed(self, e: Exception) -> None:
        first, longest, lasting = RETRY_SECONDS
        if getattr(e, "lasting", False) or isinstance(e, pairing.RemoteError):
            self._backoff = lasting
        else:
            self._backoff = min(max(self._backoff * 2, first), longest)
        self.retry_at = time.monotonic() + self._backoff
        if str(e) != self.error:
            log.warn(f"library: sync failed: {e}. Trying again in {self._backoff / 60:g} min, "
                     "or on a new commit or a change in the library.")
        self.error = str(e)

    def sync_for_launch(self, timeout: float) -> str:
        """`sync_for_launch` on this link. While a failed sync waits to be
        tried again, a launch doesn't wait on another."""
        if self.error and time.monotonic() < self.retry_at:
            return f"library: couldn't sync ({self.error}); launching the local copy"
        with self._launch_lock:
            attempt = self._launch
            if attempt is None or attempt.done.is_set():
                attempt = self._launch = _Attempt()
                threading.Thread(target=self._launch_sync, args=(attempt,), daemon=True).start()
        if not attempt.done.wait(timeout):
            return "library: no answer in time; launching the local copy"
        if attempt.error:
            return f"library: couldn't sync ({attempt.error}); launching the local copy"
        return ""

    def _launch_sync(self, attempt: _Attempt) -> None:
        try:
            self.announce_soon(self.sync_now())
        except (LibraryError, pairing.RemoteError, OSError) as e:
            attempt.error = str(e)
        finally:
            attempt.done.set()

    def announce_soon(self, synced: Synced) -> None:
        """`_announce`, from a thread other than the link's."""
        if (synced.pushed or synced.tell_held) and self._loop is not None \
                and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._announce(synced), self._loop)

    async def _announce(self, synced: Synced) -> None:
        if synced.tell_held and synced.held is not None:
            self._told_held = True
            await self._send({"type": "library_held",
                              "files": [f.to_json() for f in synced.held]})
        if synced.pushed:
            await self._send({"type": "library_pushed", "head": synced.pushed})


__all__ = ["KEEP_TREES", "LAUNCH_SYNC_SECONDS", "LibraryError", "LibraryLink", "LibrarySync",
           "MAX_BUILTINS", "MAX_BUILTIN_BYTES", "PUSHED_REF", "RETRY_SECONDS", "Synced",
           "TREES_DIR", "Token", "builtins_message", "commit_tree", "conflict_files",
           "conflict_note", "console_token", "head_stamp", "label", "launch_index", "library_tree",
           "on_host", "pending_conflict", "pending_held", "report", "sync_for_launch"]
