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

The host pushes its `main` to `hosts/<host id>` and fetches `main` by name; it
never merges, only fast-forwards, under the lock bot-bottle's own commits take
(`versions.writing`). Git runs against the library URL with a short-lived
token from the console (`POST /api/v1/hosts/library-token`), handed to git by
an inline credential helper, for the library's host only, that reads it from
the child's environment, so no remote or token is written to the repo's
config. Every fast-forward is a `library.updated` journal event.

A failed sync is tried again later (sooner on a new commit or `library_moved`),
after a longer wait for a refusal that won't pass by itself: a deleted account
(403) or a push past the library's size limits (413).

A console launch names the library commit it runs (`library_commit`): the
host fetches `main` if it lacks that commit, then resolves library bottles and
agents from a copy of that commit's `agents/` and `bottles/` (`tree_at`)
first, its own copy only for names the commit lacks. The repo's working tree
and `main` are untouched, and a host whose `main` diverged still launches the
console's version.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ... import run_bottle
from ...contract.hostctl import LIBRARY_COMMIT, LaunchRequest
from ...lib import log
from ...manifest.versions import TRACKED, VersionError, git_env, launch_versions, writing
from ...paths import bot_bottle_root, host_db_path, private_dir
from . import pairing
from .library_conflict import Conflict, LibraryConflictStore

if TYPE_CHECKING:
    from ...manifest import ManifestIndex

TOKEN_PATH = "/api/v1/hosts/library-token"
REMOTE_REF = "refs/remotes/library/main"
# How long a CLI launch waits for a sync before it uses the local copy.
LAUNCH_SYNC_SECONDS = 2.0
_TOKEN_SLACK = 60.0  # seconds before expiry a token is replaced
# Where launches' commit copies are kept (under the root, outside the
# tracked directories), and how many. One used within the grace period stays
# too: a launch may still be reading it.
TREES_DIR = "library-commits"
KEEP_TREES = 8
TREE_GRACE_SECONDS = 3600.0
_TREE_MODES = ("100644", "100755", "120000")  # a symlink is a file holding its target
# The longest a fetch or push may take, and how long before git gives up on a
# transfer slower than a kilobyte a second.
NETWORK_SECONDS = 60.0
_LOW_SPEED_SECONDS = 20
# The wait before trying a failed sync again: doubling from the first to the
# second, or the last after a refusal that won't pass by itself.
RETRY_SECONDS = (30.0, 600.0, 3600.0)
# Answers git's credential `get` from the child's environment, never the config.
_HELPER = ('!f() { test "$1" = get && '
           'printf "username=%s\\npassword=%s\\n" "$BB_LIBRARY_USER" "$BB_LIBRARY_TOKEN"; }; f')
# Whatever the user's git config says: no hooks, signing or fsmonitor.
_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "core.fsmonitor=false")
# And talking to the library: no redirects (so the token goes nowhere else),
# every fetched object checked, and a stalled transfer dropped.
_NETWORK = ("-c", "http.followRedirects=false", "-c", "fetch.fsckObjects=true",
            "-c", "http.lowSpeedLimit=1000", "-c", f"http.lowSpeedTime={_LOW_SPEED_SECONDS}")


class LibraryError(Exception):
    """The library couldn't be reached or synced. `lasting`: a refusal that
    won't pass by itself (a deleted account, a push too large), so it isn't
    retried soon."""

    def __init__(self, message: str, *, lasting: bool = False) -> None:
        super().__init__(message)
        self.lasting = lasting


@dataclass(frozen=True)
class Token:
    url: str
    username: str
    token: str
    expires: float  # time.time()


def _post(url: str, body: dict[str, object], bearer: str = "",
          timeout: float = 10.0) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        reply = json.load(resp)
    if not isinstance(reply, dict):
        raise LibraryError("the console's reply isn't an object")
    return reply


def _expiry(raw: object) -> float:
    from datetime import datetime  # pylint: disable=import-outside-toplevel
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 300.0


def _library_url(raw: object) -> str:
    """The console's library URL, if it's one git should be handed: HTTPS (or
    HTTP to this machine, for a dev console). Never an option or a transport
    that runs a command."""
    url = str(raw)
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if not parts.hostname or not (parts.scheme == "https" or (parts.scheme == "http" and local)):
        raise LibraryError(f"the console's library URL isn't HTTPS: {url!r}", lasting=True)
    return url


def console_token(timeout: float = 10.0) -> Token:
    """A library token from the console, with the host's access token,
    refreshed once if the console says it expired."""
    paired = pairing.load_pairing().require()
    base = paired.url.rstrip("/")
    access, refresh = paired.tokens()
    try:
        try:
            reply = _post(base + TOKEN_PATH, {}, access, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 401 or not refresh:
                raise
            access = str(_post(f"{base}/api/v1/hosts/token/refresh",
                               {"refresh_token": refresh}, timeout=timeout)["access_token"])
            pairing.store().update({"access_token": access})
            reply = _post(base + TOKEN_PATH, {}, access, timeout)
    except urllib.error.HTTPError as e:
        # 401 once refreshed: a deleted account, or this host's pairing
        # revoked. 404: the console's library is off. Neither passes soon.
        raise LibraryError(f"couldn't get a library token: {e}",
                           lasting=e.code in (401, 403, 404)) from e
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise LibraryError(f"couldn't get a library token: {e}") from e
    try:
        return Token(url=_library_url(reply["url"]),
                     username=str(reply.get("username") or "host"),
                     token=str(reply["token"]), expires=_expiry(reply.get("expires_at")))
    except KeyError as e:
        raise LibraryError(f"the console's library token lacks {e}") from e


def _failure(verb: str, stderr: str) -> LibraryError:
    """What git's stderr (in the C locale) says went wrong, for the log."""
    text = stderr.strip()
    if "error: 403" in text:
        return LibraryError("the library refused this host (403): was the account deleted? "
                            f"git {verb}: {text}", lasting=True)
    if "error: 413" in text or "HTTP 413" in text:
        return LibraryError("too large for the library (413): remove something big from "
                            f"agents/ or bottles/. git {verb}: {text}", lasting=True)
    if "remote rejected" in text or "unpacker error" in text:
        # Its own checks (`transfer.fsckObjects`, the branch hook): this
        # history won't be taken as it is.
        return LibraryError(f"the library rejected this host's push: {text}", lasting=True)
    return LibraryError(f"git {verb}: {text}")


def _auth_failed(stderr: str) -> bool:
    return "Authentication failed" in stderr or "error: 401" in stderr


def _credential_config(url: str) -> tuple[str, ...]:
    """The helper, for the library's own scheme and host only (none of the
    user's helpers): git asks no other host for it."""
    parts = urlsplit(url)
    scope = f"{parts.scheme}://{parts.netloc}." if parts.scheme in ("http", "https") else ""
    return ("-c", "credential.helper=", "-c", f"credential.{scope}helper={_HELPER}")


def _run(argv: list[str], env: dict[str, str],
         timeout: float | None) -> subprocess.CompletedProcess[str]:
    """`argv`, and once `timeout` passes, it and what it started (git's
    remote helper, which would hold the pipes open) are killed."""
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env,
                          start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise LibraryError(f"no answer from the library in {timeout:.0f}s") from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def head_stamp(root: Path) -> float:
    """When the repo's refs last changed: a new local commit moves it."""
    git = root / ".git"
    stamps = [p.stat().st_mtime for p in (git / "HEAD", git / "refs" / "heads",
                                          git / "packed-refs", git / "logs" / "HEAD")
              if p.exists()]
    return max(stamps, default=0.0)


@dataclass
class Synced:
    """What a sync did: the head pushed (to wait on the console's merge), and
    whether `main` moved here. `main`: the library's `main` it fetched.
    `stamp`: `head_stamp` once it had, so its own commits don't look like new
    ones."""

    pushed: str = ""
    moved: bool = False
    main: str = field(default="", compare=False)
    stamp: float = field(default=0.0, compare=False)


@dataclass
class LibrarySync:
    """Blocking git work on the library repo, one sync at a time."""

    host_id: str
    token_source: Callable[[], Token] = console_token
    audit: Callable[[], Any] = lambda: None
    root: Path = field(default_factory=bot_bottle_root)
    _token: Token | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _credential(self) -> Token:
        if self._token is None or self._token.expires - _TOKEN_SLACK <= time.time():
            self._token = self.token_source()
        return self._token

    def _git(self, *args: str, token: Token | None = None, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        env = git_env()
        env["LC_ALL"] = "C"  # its messages are read (`_failure`)
        argv = ["git", "-C", str(self.root), *_SAFE]
        if token is not None:  # the token only in this child's environment
            env.update(BB_LIBRARY_USER=token.username, BB_LIBRARY_TOKEN=token.token)
            argv += [*_NETWORK, *_credential_config(token.url)]
        result = _run([*argv, *args], env, timeout)
        if check and result.returncode != 0:
            raise _failure(args[0], result.stderr or result.stdout)
        return result

    def _rev(self, ref: str) -> str:
        out = self._git("rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
        return out.stdout.strip() if out.returncode == 0 else ""

    def _remote(self, *args: str) -> tuple[Token, subprocess.CompletedProcess[str]]:
        """A fetch or push, again with a new token if the library refused this
        one (its clock, say, or a new console key)."""
        token = self._credential()
        result = self._git(*args, token=token, check=False, timeout=NETWORK_SECONDS)
        if result.returncode != 0 and _auth_failed(result.stderr):
            self._token = None
            token = self._credential()
            result = self._git(*args, token=token, check=False, timeout=NETWORK_SECONDS)
            if result.returncode != 0 and _auth_failed(result.stderr):
                self._token = None
        return token, result

    def _fetch_main(self) -> Token:
        """The library's `main` into REMOTE_REF (none while the library has
        no `main` yet), by name: the library serves protocol v0 only. The
        token it used."""
        token = self._credential()
        token, fetched = self._remote("fetch", "-q", "--no-tags", token.url,
                                      f"+refs/heads/main:{REMOTE_REF}")
        if fetched.returncode != 0:
            if "couldn't find remote ref" not in fetched.stderr:
                raise _failure("fetch", fetched.stderr)
            self._git("update-ref", "-d", REMOTE_REF, check=False)
        return token

    def sync(self) -> Synced:
        """Fetch `main`, commit hand edits, then fast-forward to `main` or push
        this host's commits to its branch. Only the fast-forward holds the
        versions lock, so a slow library never holds up a bot-bottle write."""
        with self._lock:
            if not (self.root / ".git").is_dir():
                return Synced()
            token = self._fetch_main()
            try:
                with writing(self.root) as local:
                    remote = self._rev(REMOTE_REF)
                    if remote and remote != local and (not local or self.contains(remote, local)):
                        self._fast_forward(local, remote)
                        return Synced(moved=True, main=remote, stamp=head_stamp(self.root))
                    stamp = head_stamp(self.root)
            except VersionError as e:
                raise LibraryError(str(e)) from e
            if local and local != remote:
                # The commit, not HEAD: one made since is the next sync's.
                # Also while a conflict is pending: the console merges the
                # branch whole once it's resolved.
                _token, pushed = self._remote("push", "-q", token.url,
                                              f"{local}:refs/heads/hosts/{self.host_id}")
                if pushed.returncode != 0:
                    raise _failure("push", pushed.stderr or pushed.stdout)
                return Synced(pushed=local, main=remote, stamp=stamp)
            return Synced(main=remote, stamp=stamp)

    def heads(self) -> tuple[str, str]:
        """This host's `main` and the library's as last fetched ("" for
        none)."""
        if not (self.root / ".git").is_dir():
            return "", ""
        return self._rev("HEAD"), self._rev(REMOTE_REF)

    def ensure_commit(self, sha: str) -> None:
        """Have the commit `sha`, in the library's `main` or this host's
        history, fetching `main` for it if it isn't (only the remote ref
        moves). LibraryError if it's still not there: the console names
        commits of `main`, never any other object."""
        if not LIBRARY_COMMIT.fullmatch(sha):
            raise LibraryError(f"{sha!r} isn't a commit sha")
        with self._lock:
            if not (self.root / ".git").is_dir():
                raise LibraryError(f"this host has no library repo at {self.root}")
            if self._reachable(sha):
                return
            try:
                self._fetch_main()
            except pairing.RemoteError as e:
                raise LibraryError(f"couldn't get a library token: {e}") from e
            except LibraryError as e:
                raise LibraryError(f"couldn't fetch library commit {sha[:12]}: {e}") from e
            if not self._reachable(sha):
                raise LibraryError(f"library commit {sha[:12]} isn't on this host or in the "
                                   "account's main; refresh the console and launch again")

    def _reachable(self, sha: str) -> bool:
        return self._rev(sha) == sha and any(
            self._rev(ref) and self.contains(ref, sha) for ref in (REMOTE_REF, "HEAD"))

    def tree_at(self, sha: str) -> Path:
        """The library's `agents/` and `bottles/` as of commit `sha` (fetched
        if missing), copied into a directory of their own: what a launch at
        that commit resolves library bottles and agents from."""
        self.ensure_commit(sha)
        with _TREES:
            trees = private_dir(self.root / TREES_DIR)
            tree = trees / sha
            if not tree.is_dir():
                self._copy_tree(sha, trees, tree)
            os.utime(tree)
            _prune(trees, keep=tree)
        return tree

    def _copy_tree(self, sha: str, trees: Path, tree: Path) -> None:
        """Write `sha`'s tracked files into `tree`, whole or not at all. Only
        files: no symlinks (as `_fast_forward` checks them out), no
        submodules, and nothing outside it."""
        staging = Path(tempfile.mkdtemp(dir=trees, prefix=".new-"))
        try:
            listed = self._git("ls-tree", "-r", "-z", sha, "--", *TRACKED).stdout
            for entry in listed.split("\0"):
                meta, _, rel = entry.partition("\t")
                mode, kind, oid = (meta.split(" ") + ["", "", ""])[:3]
                parts = rel.split("/")
                if mode not in _TREE_MODES or kind != "blob" or parts[0] not in TRACKED \
                        or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
                    continue
                target = staging.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self._blob(oid))
            for directory in TRACKED:
                (staging / directory).mkdir(exist_ok=True)
            try:
                staging.rename(tree)
            except OSError:
                if not tree.is_dir():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _blob(self, oid: str) -> bytes:
        """A blob's bytes, exactly (`_git` reads text)."""
        result = subprocess.run(["git", "-C", str(self.root), *_SAFE, "cat-file", "blob", oid],
                                capture_output=True, env=git_env(), check=False)
        if result.returncode != 0:
            raise LibraryError(f"git cat-file: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def contains(self, newer: str, older: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer,
                         check=False).returncode == 0

    def _fast_forward(self, old: str, new: str) -> None:
        # Onto an unborn branch too: a repo with no commits takes the library.
        # A symlink checks out as a file holding its target, so nothing the
        # library holds points bot-bottle's reads and writes outside the repo.
        self._git("-c", "core.symlinks=false", "merge", "--ff-only", "-q", new)
        self._journal(old, new)

    def _journal(self, old: str, new: str) -> None:
        if old:
            listed = self._git("diff", "--no-renames", "--name-status", "-z", old, new,
                               "--", *TRACKED).stdout.split("\0")
            changes = list(zip(listed[0::2], listed[1::2]))
        else:
            changes = [("A", path) for path in self._git(
                "ls-tree", "-r", "-z", "--name-only", new, "--", *TRACKED).stdout.split("\0")
                       if path]
        span = f"{old}..{new}" if old else new
        authors = sorted(set(self._git("log", "--format=%an", span).stdout.splitlines()))
        audit = self.audit()
        if audit is not None and hasattr(audit, "library_updated"):
            audit.library_updated(
                old=old, new=new, changed=[path for _, path in changes], authors=authors,
                added=[path for how, path in changes if how == "A"],
                deleted=[path for how, path in changes if how == "D"])


_TREES = threading.Lock()  # making and pruning commit copies


def _prune(trees: Path, *, keep: Path) -> None:
    """Remove all but `keep` and the newest copies (`KEEP_TREES` in all),
    sparing any used within `TREE_GRACE_SECONDS`, and staging left behind by
    a crash."""
    recent = time.time() - TREE_GRACE_SECONDS
    found = [(p.stat().st_mtime, p) for p in trees.iterdir() if p != keep]
    copies = sorted(((m, p) for m, p in found if not p.name.startswith(".")), reverse=True)
    stale = [p for m, p in found if p.name.startswith(".") and m < recent]
    stale += [p for m, p in copies[KEEP_TREES - 1:] if m < recent]
    for old in stale:
        shutil.rmtree(old, ignore_errors=True)


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
    return conflict_note(conflict) if conflict is not None else note


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


def _names(files: tuple[str, ...]) -> str:
    """`bottles/dev.md` as `dev`, `agents/x.md` as `agent x`, others as is."""
    names = [f"agent {f[7:-3]}" if f.startswith("agents/") and f.endswith(".md")
             else f[8:-3] if f.startswith("bottles/") and f.endswith(".md") else f
             for f in files]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def conflict_note(conflict: Conflict) -> str:
    """The line a CLI launch shows while a conflict is pending."""
    host = socket.gethostname().split(".")[0] or "this host"
    what = (f"{_names(conflict.files)} {'has' if len(conflict.files) == 1 else 'have'}"
            if conflict.files else "this host's library changes have")
    return f"library: {what} a conflict waiting in the console; launching {host}'s version"


def report() -> dict[str, object]:
    """The library here, for `bot-bottle status`: `state` "conflict" (with
    `files` and `since`), "off", or "synced" or "syncing" (with `head` and
    the library's `main` as last fetched)."""
    conflict = pending_conflict()
    if conflict is not None:
        return {"state": "conflict", "files": list(conflict.files), "since": conflict.since}
    link = _active
    if link is None or not link.enabled:
        return {"state": "off"}
    try:
        head, main = link.syncer().heads()
    except (pairing.RemoteError, OSError):
        head, main = "", ""
    return {"state": "synced" if head and head == main else "syncing",
            "head": head, "main": main}


def _launch_sync() -> LibrarySync:
    """The active link's sync (one git job at a time), else one for this
    host's pairing, whose token is asked for only if a fetch is needed."""
    if _active is not None:
        return _active.syncer()
    return LibrarySync(host_id="")


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
    built-ins resolve as ever."""
    commit = request.library_commit
    tree = _launch_sync().tree_at(commit) if commit else None
    versions = launch_versions(cwd, host=commit)
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
                 conflicts: LibraryConflictStore | None = None) -> None:
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
                self.schedule()
            else:  # off: nothing to resolve (on again, the console says anew)
                await asyncio.to_thread(self.conflicts().clear)
        elif kind == "library_moved" and self.enabled:
            self.schedule()
        elif kind == "library_conflict":
            files = msg.get("files")
            await asyncio.to_thread(
                self._record_conflict, [str(f) for f in files] if isinstance(files, list) else [])

    def _record_conflict(self, files: list[str]) -> None:
        """Keep the conflict, and the commit this host had, which `main`
        contains once it's resolved."""
        try:
            tip = self.syncer().heads()[0]
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
        """On the link's watch loop: a new local commit is synced, and a
        failed sync tried again once its wait is over."""
        if not self.enabled:
            return
        stamp = await asyncio.to_thread(head_stamp, self._root or bot_bottle_root())
        due = bool(self.retry_at) and time.monotonic() >= self.retry_at
        if stamp != self._seen or due:
            self._seen = stamp
            self.retry_at = 0.0
            self.schedule()

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
        once the fetched `main` contains the commit this host had then."""
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
            if pending is not None and (synced.moved or (
                    synced.main and pending.tip and sync.contains(synced.main, pending.tip))):
                self.conflicts().clear()
                log.info("library: the conflict was resolved in the console")
        except (OSError, sqlite3.Error) as e:
            log.warn(f"library: couldn't check the pending conflict: {e}")
        return synced

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
        if synced.pushed and self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._announce(synced), self._loop)

    async def _announce(self, synced: Synced) -> None:
        if synced.pushed:
            await self._send({"type": "library_pushed", "head": synced.pushed})


__all__ = ["KEEP_TREES", "LAUNCH_SYNC_SECONDS", "LibraryError", "LibraryLink", "LibrarySync",
           "NETWORK_SECONDS", "RETRY_SECONDS", "Synced", "TREES_DIR", "TREE_GRACE_SECONDS",
           "Token", "commit_tree", "conflict_note", "console_token", "head_stamp",
           "launch_index", "pending_conflict", "report", "sync_for_launch"]
