"""A release's changes to built-ins, merged into the library's copies of them
(PRD 0131).

A copy (`copied_from: <name>@<V>`, `manifest/account_version.py`) merges the
built-in's changes once the account's version, the lock at `main`, names a
release L this host runs. The host doesn't merge: only the console writes
`main`. It proposes, as its branch, a commit U whose parent is the commit K
where the copy was made (the copy's file exactly the built-in at V, marked)
and which changes only that file, to the built-in at L (marked @L). The
console merges U like any host branch: three-way with base K, so the
release's changes and the copy's own combine, and an overlap is a conflict
resolved in the console.

U is the same commit on every host on L (fixed author, message and dates), so
hosts proposing the same merge push the same branch head. It's built without
the working tree or `main` here; the host's own work replaces it on its
branch whenever there's some, and U is proposed again once that's merged.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ...manifest import parse_check
from ...manifest.account_version import LOCK_FILE, copied_from, lock_version, mark, packaged
from ...manifest.versions import TRACKED
from .builtins_cache import BuiltinUnavailable, Builtins

EMAIL = "bot-bottle@localhost"


class UpstreamError(Exception):
    """git failed building a proposal."""


@dataclass(frozen=True)
class Repo:
    """The library repo, and the environment git runs in there (the
    caller's, less any GIT_*: `LibrarySync`'s)."""

    root: Path
    env: Mapping[str, str]


@dataclass(frozen=True)
class Proposal:
    """The commit `sha` merging release `version`'s built-in into `path`."""

    sha: str
    path: str
    version: str


@dataclass(frozen=True)
class Upstream:
    """What a look at `main` found: the next proposal, if any, and why any
    copy before it can't merge yet (each a line for `bot-bottle status`)."""

    proposal: Proposal | None = None
    notes: tuple[str, ...] = ()


def _run(repo: Repo, args: tuple[str, ...], stdin: bytes | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    # As library_git runs git: no hooks or signing, and UTF-8 messages (an
    # encoding header would change U's sha).
    return subprocess.run(
        ["git", "-C", str(repo.root), "-c", "core.hooksPath=/dev/null",
         "-c", "commit.gpgsign=false", "-c", "i18n.commitEncoding=UTF-8", *args],
        input=stdin, capture_output=True, env={**repo.env, **(env or {})}, check=False)


def _git(repo: Repo, *args: str, stdin: str | None = None,
         env: dict[str, str] | None = None, check: bool = True) -> str:
    result = _run(repo, args, stdin.encode("utf-8") if stdin is not None else None, env)
    if check and result.returncode != 0:
        raise UpstreamError(f"git {args[0]}: "
                            f"{(result.stderr or result.stdout).decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="replace")


def _show(repo: Repo, sha: str, rel: str) -> str | None:
    """`rel`'s text at `sha`, exactly; None if it isn't there (or isn't text)."""
    shown = _run(repo, ("show", f"{sha}:{rel}"))
    if shown.returncode != 0:
        return None
    try:
        return shown.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def account_version(repo: Repo, main: str) -> str:
    """The release the lock at `main` names; "" for none."""
    text = _show(repo, main, LOCK_FILE)
    return lock_version(text) if text is not None else ""


def copies(repo: Repo, main: str) -> list[str]:
    """The library files at `main` that may be copies of built-ins."""
    found = _git(repo, "grep", "-l", "--full-name", "-e", "^copied_from:", main, "--",
                 *TRACKED, check=False)
    prefix = f"{main}:"
    return sorted(p.removeprefix(prefix) for p in found.splitlines()
                  if parse_check.checked(p.removeprefix(prefix)))


def base_commit(repo: Repo, main: str, rel: str, content: str) -> str:
    """The newest commit in `main`'s history whose `rel` is exactly
    `content`: where the copy was made. "" if there's none."""
    oid = _git(repo, "hash-object", "--no-filters", "--stdin", stdin=content).strip()
    shas = _git(repo, "log", "--format=%H", main, "--", rel).split()
    if not shas:
        return ""
    found = _git(repo, "cat-file", "--batch-check=%(objectname)",
                 stdin="".join(f"{sha}:{rel}\n" for sha in shas)).splitlines()
    return next((sha for sha, blob in zip(shas, found) if blob == oid), "")


def upstream_commit(repo: Repo, base: str, rel: str, content: str, version: str) -> str:
    """U: `base`'s tree with `rel` as `content`, on `base`, as bot-bottle
    `version`, dated as `base` was committed, so every host makes the same
    commit. Written to the repo's objects only."""
    mode = _git(repo, "ls-tree", base, "--", rel).split(" ", 1)[0] or "100644"
    blob = _git(repo, "hash-object", "-w", "--no-filters", "--stdin", stdin=content).strip()
    with tempfile.TemporaryDirectory(prefix="bb-upstream-") as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", base, env=index)
        _git(repo, "update-index", "--cacheinfo", f"{mode},{blob},{rel}", env=index)
        tree = _git(repo, "write-tree", env=index).strip()
    when = "@" + _git(repo, "show", "-s", "--format=%cd", "--date=raw", base).strip()
    who = f"bot-bottle {version}"
    kind, name = rel.split("/", 1)
    env = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": EMAIL, "GIT_AUTHOR_DATE": when,
           "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": EMAIL, "GIT_COMMITTER_DATE": when}
    message = f"bot-bottle {version}: {kind[:-1]} {name.removesuffix('.md')}"
    return _git(repo, "commit-tree", tree, "-p", base, "-m", message, env=env).strip()


def _contains(repo: Repo, newer: str, older: str) -> bool:
    return _run(repo, ("merge-base", "--is-ancestor", older, newer)).returncode == 0


def next_upstream(repo: Repo, main: str, installed: str, builtins: Builtins) -> Upstream:
    """The next copy's proposal at `main`, for a host running release
    `installed`: only when the lock at `main` names exactly that release,
    whose built-ins this host has. One that `main` already holds (merged, or
    resolved in the console) isn't proposed again."""
    version = account_version(repo, main)
    if not version or version != installed:
        return Upstream()
    notes: list[str] = []
    for rel in copies(repo, main):
        content = _show(repo, main, rel)
        marker = copied_from(content) if content is not None else None
        if marker is None or marker[1] == version:
            continue
        name, old = marker
        kind = rel.split("/", 1)[0]
        new = packaged(kind, name)
        if new is None:  # not a built-in any more: the copy is the user's now
            continue
        try:
            before = builtins.at(kind, name, old)
        except BuiltinUnavailable as e:
            notes.append(f"{rel}: couldn't get bot-bottle {old}'s {name} to merge {version}'s "
                         f"changes into it ({e}); trying again on the next sync")
            continue
        if before is None:
            notes.append(f"{rel}: bot-bottle {old} has no built-in {name}; not merging")
            continue
        base = base_commit(repo, main, rel, mark(before, name, old))
        if not base:
            notes.append(f"{rel}: no commit holds it as copied from {name}@{old}; "
                         f"not merging bot-bottle {version}'s changes")
            continue
        sha = upstream_commit(repo, base, rel, mark(new, name, version), version)
        if not _contains(repo, main, sha):
            return Upstream(Proposal(sha, rel, version), tuple(notes))
    return Upstream(notes=tuple(notes))


__all__ = ["EMAIL", "Proposal", "Repo", "Upstream", "UpstreamError", "account_version",
           "base_commit", "copies", "next_upstream", "upstream_commit"]
