"""The library's git work (PRD 0131): this host's `~/.bot-bottle` repo
against the account's repo in the console's library app. `library_link` runs
it on the console link.

The host pushes its `main` to `hosts/<host id>` and fetches `main` by name; it
never merges, only fast-forwards, under the lock bot-bottle's own commits take
(`versions.writing`). Git runs against the library URL with a short-lived
token from the console (`POST /api/v1/hosts/library-token`), handed to git by
an inline credential helper, for the library's host only, that reads it from
the child's environment, so no remote or token is written to the repo's
config. Every fast-forward is a `library.updated` journal event. Before a
push, the files it would bring to `main` must parse (`unparseable`), else
it's held back.

Once this host's branch is merged and its `main` is the library's, it may
propose a release's changes to a copy of a built-in as its branch instead
(`library_upstream.py`): pushed forced, as is its own next push over it. A
push of its own `main` that the branch refuses as not a fast-forward (the
repo made anew, a reset) is made again forced: the branch is this host's
alone, and what it pushes was checked first.

A console launch's commit is copied out of the repo (`tree_at`), so its
working tree and `main` are untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...contract.hostctl import LIBRARY_COMMIT
from ...lib import log
from ...manifest.versions import LOCK, TRACKED, VersionError, git_env, writing
from ...paths import bot_bottle_root, private_dir
from . import pairing
from .builtins_cache import Builtins, installed_release
from .library_held import HeldFile, unparseable
from .library_upstream import Repo, UpstreamError, next_upstream

TOKEN_PATH = "/api/v1/hosts/library-token"
REMOTE_REF = "refs/remotes/library/main"
# The commit this host last pushed: later ones are checked before a push.
PUSHED_REF = "refs/library/pushed"
# The release's merge into a copy this host last proposed (`library_upstream`),
# while it's what the branch holds.
UPSTREAM_REF = "refs/library/upstream"
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


def _not_ahead(stderr: str) -> bool:
    """Whether git refused a push only for not being a fast-forward."""
    return "[rejected]" in stderr and ("(non-fast-forward)" in stderr
                                       or "(fetch first)" in stderr)


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
    ones. `held`: what it didn't push because it doesn't parse (None: not
    checked), and `tell_held` whether the console should hear of it.
    `replaced`: the push replaced what this host's branch held (a proposal,
    or history its `main` doesn't build on), forced."""

    pushed: str = ""
    moved: bool = False
    main: str = field(default="", compare=False)
    stamp: float = field(default=0.0, compare=False)
    held: tuple[HeldFile, ...] | None = field(default=None, compare=False)
    tell_held: bool = field(default=False, compare=False)
    replaced: bool = field(default=False, compare=False)


@dataclass
class LibrarySync:
    """Blocking git work on the library repo, one sync at a time."""

    host_id: str
    token_source: Callable[[], Token] = console_token
    audit: Callable[[], Any] = lambda: None
    root: Path = field(default_factory=bot_bottle_root)
    installed: Callable[[], str] = installed_release
    builtins: Builtins = field(default_factory=Builtins)
    # Why copies of built-ins don't merge a release's changes yet.
    upstream_notes: tuple[str, ...] = ()
    _token: Token | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _credential(self) -> Token:
        if self._token is None or self._token.expires - _TOKEN_SLACK <= time.time():
            self._token = self.token_source()
        return self._token

    def repo(self) -> Repo:
        """The repo, for `library_upstream`'s git."""
        return Repo(self.root, git_env())

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
        this host's commits to its branch, unless one changed since the last
        push doesn't parse (`held`). Only the fast-forward holds the versions
        lock, so a slow library never holds up a bot-bottle write."""
        with self._lock:
            if not (self.root / ".git").is_dir():
                return Synced()
            token = self._fetch_main()
            try:
                with writing(self.root) as local:
                    remote = self._rev(REMOTE_REF)
                    moved = bool(remote) and remote != local and (
                        not local or self.contains(remote, local))
                    if moved:
                        self._fast_forward(local, remote)
                    stamp = head_stamp(self.root)
            except VersionError as e:
                raise LibraryError(str(e)) from e
            if moved:  # a proposal, if any, once the versions lock is let go
                return Synced(moved=True, main=remote, stamp=stamp, held=(),
                              pushed=self._upstream(remote))
            if local and local != remote:
                held = self.unparseable(local, remote)
                if held:
                    return Synced(main=remote, stamp=stamp, held=held)
                # The commit, not HEAD: one made since is the next sync's.
                # Also while a conflict is pending: the console merges the
                # branch whole once it's resolved. Forced only over a
                # proposal, which is recomputed from `main` once this merges.
                proposal = self._rev(UPSTREAM_REF)
                replaced = bool(proposal) and not self.contains(local, proposal)
                spec = f"{local}:refs/heads/hosts/{self.host_id}"
                _token, pushed = self._remote("push", "-q", token.url,
                                              f"+{spec}" if replaced else spec)
                if pushed.returncode != 0 and not replaced and _not_ahead(pushed.stderr):
                    # The branch holds history this repo doesn't build on (a
                    # repo made anew, a reset, a re-pair onto another host's
                    # branch): this host's checked `main` replaces it.
                    log.info(f"library: replacing this host's branch, which holds history "
                             f"its main doesn't ({local[:12]})")
                    _token, pushed = self._remote("push", "-q", token.url, f"+{spec}")
                    replaced = True  # and any conflict on what it held goes with it
                if pushed.returncode != 0:
                    raise _failure("push", pushed.stderr or pushed.stdout)
                self._git("update-ref", PUSHED_REF, local)
                if proposal:
                    self._git("update-ref", "-d", UPSTREAM_REF)
                return Synced(pushed=local, main=remote, stamp=stamp, held=(), replaced=replaced)
            return Synced(main=remote, stamp=stamp, held=(),
                          pushed=self._upstream(remote) if remote else "")

    def _upstream(self, main: str) -> str:
        """Propose the next copy's merge of a release's built-in as this
        host's branch, once `main` holds what the branch has: the commit
        pushed, or "" for none."""
        for ref in (UPSTREAM_REF, PUSHED_REF):
            tip = self._rev(ref)
            if tip and not self.contains(main, tip):
                return ""  # this host's branch waits to merge first
        try:
            found = next_upstream(self.repo(), main, self.installed(), self.builtins)
        except UpstreamError as e:
            self._notes((f"couldn't merge a release's built-ins into copies: {e}",))
            return ""
        self._notes(found.notes)
        if found.proposal is None:
            return ""
        sha = found.proposal.sha
        try:
            token = self._credential()
            _token, pushed = self._remote("push", "-q", token.url,
                                          f"+{sha}:refs/heads/hosts/{self.host_id}")
            if pushed.returncode != 0:
                raise _failure("push", pushed.stderr or pushed.stdout)
        except LibraryError as e:  # a library app that takes no forced push, say
            self._notes((*found.notes, f"{found.proposal.path}: couldn't propose bot-bottle "
                         f"{found.proposal.version}'s changes to it ({e})"))
            return ""
        self._git("update-ref", UPSTREAM_REF, sha)
        log.info(f"library: proposed bot-bottle {found.proposal.version}'s changes to "
                 f"{found.proposal.path} ({sha[:12]})")
        return sha

    def _notes(self, notes: tuple[str, ...]) -> None:
        if notes != self.upstream_notes:
            for note in notes:
                log.warn(f"library: {note}")
        self.upstream_notes = notes

    def branch_tip(self) -> str:
        """What this host's branch holds as last pushed: a proposal, or its
        `main` ("" for none)."""
        if not (self.root / ".git").is_dir():
            return ""
        return self._rev(UPSTREAM_REF) or self._rev("HEAD")

    def unparseable(self, local: str, remote: str) -> tuple[HeldFile, ...]:
        """The bottle and agent files a push of `local` would bring to `main`
        that don't parse there: every file that differs from where it and
        `main` part (all of them, with no `main` or nothing shared), but one
        that's as it was in the last commit pushed, which was checked then.
        By content, not by commits: so an older bad commit under newer ones
        is held, and a reset or rewritten history can't skip one."""
        base = self._git("merge-base", local, remote, check=False).stdout.strip() \
            if remote else ""
        changed = (self._changed(base, local) if base
                   else self._paths("ls-tree", "-r", "-z", "--name-only", local))
        pushed = self._rev(PUSHED_REF)
        if pushed and changed:
            changed &= self._changed(pushed, local)
        return unparseable(sorted(changed), lambda rel: self._show(local, rel))

    def _changed(self, older: str, newer: str) -> set[str]:
        return self._paths("diff", "--no-renames", "--name-only", "-z", older, newer)

    def _paths(self, *args: str) -> set[str]:
        """The tracked paths a `diff` or `ls-tree` lists (`-z`)."""
        listed = self._git(*args, "--", *TRACKED).stdout
        return {path for path in listed.split("\0") if path}

    def _show(self, sha: str, rel: str) -> bytes | None:
        """`rel`'s bytes at `sha`; None when it isn't there (deleted)."""
        try:
            return self._blob(f"{sha}:{rel}")
        except LibraryError:
            return None

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
            listed = self._git("ls-tree", "-r", "-z", sha, "--", *TRACKED, LOCK).stdout
            for entry in listed.split("\0"):
                meta, _, rel = entry.partition("\t")
                mode, kind, oid = (meta.split(" ") + ["", "", ""])[:3]
                parts = rel.split("/")
                if mode not in _TREE_MODES or kind != "blob" or (
                        parts[0] not in TRACKED and parts != [LOCK]) \
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
                               "--", *TRACKED, LOCK).stdout.split("\0")
            changes = list(zip(listed[0::2], listed[1::2]))
        else:
            changes = [("A", path) for path in self._git(
                "ls-tree", "-r", "-z", "--name-only", new, "--", *TRACKED,
                LOCK).stdout.split("\0")
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


__all__ = ["KEEP_TREES", "LibraryError", "LibrarySync", "NETWORK_SECONDS", "PUSHED_REF",
           "REMOTE_REF", "Synced", "TOKEN_PATH", "TREES_DIR", "TREE_GRACE_SECONDS", "Token",
           "UPSTREAM_REF", "console_token", "head_stamp"]
