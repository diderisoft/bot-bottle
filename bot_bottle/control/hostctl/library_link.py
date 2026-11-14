"""The library (PRD 0131): this host's `~/.bot-bottle` repo synced with the
account's repo in the console's library app, while the console says so.

- `library_sync` (`enabled`), on each connect: sync only while enabled. (Not
  `library`, which is PRD 0109's request for the host's agents and bottles.)
- `library_moved` (`head`): the account's `main` moved; fetch and fast-forward.
- `library_conflict` (`files`): this host's branch didn't merge; it keeps its
  own version until the conflict is resolved in the console.
- `library_pushed` (`head`, from the host): this host pushed its branch.

The host pushes its `main` to `hosts/<host id>` and fetches `main`; it never
merges, only fast-forwards. Git runs against the library URL with a short-lived
token from the console (`POST /api/v1/hosts/library-token`), handed to git by
an inline credential helper that reads it from the child's environment, so no
remote or token is written to the repo's config. Every fast-forward is a
`library.updated` journal event.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...lib import log
from ...manifest.versions import TRACKED, VersionError, record_hand_edits
from ...paths import bot_bottle_root
from . import pairing

TOKEN_PATH = "/api/v1/hosts/library-token"
REMOTE_REF = "refs/remotes/library/main"
# How long a CLI launch waits for a sync before it uses the local copy.
LAUNCH_SYNC_SECONDS = 2.0
_TOKEN_SLACK = 60.0  # seconds before expiry a token is replaced
# Answers git's credential `get` from the child's environment, never the config.
_HELPER = ('!f() { test "$1" = get && '
           'printf "username=%s\\npassword=%s\\n" "$BB_LIBRARY_USER" "$BB_LIBRARY_TOKEN"; }; f')


class LibraryError(Exception):
    """The library couldn't be reached or synced."""


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
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise LibraryError(f"couldn't get a library token: {e}") from e
    try:
        return Token(url=str(reply["url"]), username=str(reply.get("username") or "host"),
                     token=str(reply["token"]), expires=_expiry(reply.get("expires_at")))
    except KeyError as e:
        raise LibraryError(f"the console's library token lacks {e}") from e


@dataclass
class Synced:
    """What a sync did: the head pushed (to wait on the console's merge), and
    whether `main` moved here."""

    pushed: str = ""
    moved: bool = False


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

    def _git(self, *args: str, token: Token | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        # As manifest/versions.py runs git: no hooks, signing or GIT_* from the
        # caller; and the token only in this child's environment.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        auth: tuple[str, ...] = ()
        if token is not None:
            env.update(BB_LIBRARY_USER=token.username, BB_LIBRARY_TOKEN=token.token)
            auth = ("-c", "credential.helper=", "-c", f"credential.helper={_HELPER}")
        result = subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null",
             "-c", "commit.gpgsign=false", *auth, *args],
            capture_output=True, text=True, env=env, check=False)
        if check and result.returncode != 0:
            raise LibraryError(f"git {args[0]}: {(result.stderr or result.stdout).strip()}")
        return result

    def _rev(self, ref: str) -> str:
        out = self._git("rev-parse", "--verify", "-q", ref + "^{commit}", check=False)
        return out.stdout.strip() if out.returncode == 0 else ""

    def sync(self) -> Synced:
        """Commit hand edits, fetch `main`, then fast-forward to it or push
        this host's commits to its branch."""
        with self._lock:
            if not (self.root / ".git").is_dir():
                return Synced()
            try:
                record_hand_edits(root=self.root)
            except VersionError as e:
                raise LibraryError(str(e)) from e
            token = self._credential()
            fetched = self._git("fetch", "-q", "--no-tags", token.url,
                                f"+refs/heads/main:{REMOTE_REF}", token=token, check=False)
            if fetched.returncode != 0:
                if "couldn't find remote ref" not in fetched.stderr:
                    raise LibraryError(f"git fetch: {fetched.stderr.strip()}")
                self._git("update-ref", "-d", REMOTE_REF, check=False)  # no `main` yet
            local, remote = self._rev("HEAD"), self._rev(REMOTE_REF)
            if remote and remote != local and (not local or self._contains(remote, local)):
                self._fast_forward(local, remote)
                return Synced(moved=True)
            if local and local != remote:
                self._git("push", "-q", token.url,
                          f"HEAD:refs/heads/hosts/{self.host_id}", token=token)
                return Synced(pushed=local)
            return Synced()

    def _contains(self, newer: str, older: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer,
                         check=False).returncode == 0

    def _fast_forward(self, old: str, new: str) -> None:
        # Onto an unborn branch too: a repo with no commits takes the library.
        self._git("merge", "--ff-only", "-q", new)
        self._journal(old, new)

    def _journal(self, old: str, new: str) -> None:
        changed = (self._git("diff", "--name-only", old, new, "--", *TRACKED) if old else
                   self._git("ls-tree", "-r", "--name-only", new, "--", *TRACKED)).stdout.split()
        span = f"{old}..{new}" if old else new
        authors = sorted(set(self._git("log", "--format=%an", span).stdout.splitlines()))
        audit = self.audit()
        if audit is not None and hasattr(audit, "library_updated"):
            audit.library_updated(old=old, new=new, changed=changed, authors=authors)


_active: "LibraryLink | None" = None


def sync_for_launch(timeout: float = LAUNCH_SYNC_SECONDS) -> str:
    """Sync before a CLI launch, waiting at most `timeout`: "" when the
    library is current (or not synced on this host), else a one-line note."""
    link = _active
    if link is None or not link.enabled:
        return ""
    done: list[Synced] = []
    failed: list[str] = []

    def run() -> None:
        try:
            synced = link.sync_now()
        except (LibraryError, pairing.RemoteError) as e:
            failed.append(str(e))
            return
        link.announce_soon(synced)
        done.append(synced)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if done:
        return ""
    if failed:
        return f"library: couldn't sync ({failed[0]}); launching the local copy"
    return "library: the console didn't answer in time; launching the local copy"


class LibraryLink:
    """The library's messages on one console link."""

    REQUESTS = frozenset({"library_sync", "library_moved", "library_conflict"})

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 audit: Callable[[], Any] = lambda: None,
                 make_sync: Callable[[], LibrarySync] | None = None,
                 root: Path | None = None) -> None:
        self._send = send
        self._audit = audit
        self._make_sync = make_sync
        self._root = root
        self._sync: LibrarySync | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: "asyncio.Task[None] | None" = None
        self._again = False
        self._seen: float = 0.0
        self.enabled = False
        self.conflict: list[str] = []

    def _syncer(self) -> LibrarySync:
        if self._sync is None:
            if self._make_sync is not None:
                self._sync = self._make_sync()
            else:
                paired = pairing.load_pairing().require()
                self._sync = LibrarySync(host_id=paired.host_id, audit=self._audit,
                                         root=self._root or bot_bottle_root())
        return self._sync

    def _head_stamp(self) -> float:
        """When the repo's refs last changed: a new local commit moves it."""
        git = (self._root or bot_bottle_root()) / ".git"
        stamps = [p.stat().st_mtime for p in (git / "HEAD", git / "refs" / "heads",
                                              git / "packed-refs", git / "logs" / "HEAD")
                  if p.exists()]
        return max(stamps, default=0.0)

    async def handle(self, msg: dict[str, Any]) -> None:
        global _active  # pylint: disable=global-statement
        kind = msg.get("type")
        if kind == "library_sync":
            self.enabled = msg.get("enabled") is True
            _active = self if self.enabled else (None if _active is self else _active)
            if self.enabled:
                self.schedule()
        elif kind == "library_moved" and self.enabled:
            self.schedule()
        elif kind == "library_conflict":
            files = msg.get("files")
            self.conflict = [str(f) for f in files] if isinstance(files, list) else []
            log.warn("library: this host's changes conflict with the library "
                     f"({', '.join(self.conflict) or 'files unknown'}); resolve it in the "
                     "console. This host keeps its own version meanwhile.")

    async def tick(self) -> None:
        """On the link's watch loop: a new local commit is synced."""
        if not self.enabled:
            return
        stamp = await asyncio.to_thread(self._head_stamp)
        if stamp != self._seen:
            self._seen = stamp
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
            except (LibraryError, pairing.RemoteError, OSError) as e:
                log.warn(f"library: sync failed: {e}")
                synced = Synced()
            await self._announce(synced)
            if not self._again:
                return

    def sync_now(self) -> Synced:
        """One sync, from any thread."""
        synced = self._syncer().sync()
        if synced.moved:
            self.conflict = []
        return synced

    def announce_soon(self, synced: Synced) -> None:
        """`_announce`, from a thread other than the link's."""
        if synced.pushed and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._announce(synced), self._loop)

    async def _announce(self, synced: Synced) -> None:
        if synced.pushed:
            await self._send({"type": "library_pushed", "head": synced.pushed})


__all__ = ["LAUNCH_SYNC_SECONDS", "LibraryError", "LibraryLink", "LibrarySync", "Synced",
           "Token", "console_token", "sync_for_launch"]
