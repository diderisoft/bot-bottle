"""A project's bottles and agents at a branch, for the console (PRD 0131):
the reply to `project_bottles`.

Only for a checkout this host reported, named exactly as reported. The host
fetches, best effort, then reads `.bot-bottle/` at the branch's commit with
`git ls-tree` and `git cat-file` (the blobs as committed: no symlink
followed, no filter or textconv run): nothing is checked out, committed or
stored, so the working tree is untouched.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...manifest.versions import git_env
from .checkouts import MAX_BRANCH, SAFE, CheckoutStore, git, remote_name

FETCH_SECONDS = 10.0
MAX_FILES = 200  # each of bottles and agents
MAX_FILE_BYTES = 256 * 1024
# All the files' text in one reply, as JSON: well under the link's 16 MiB
# line (200 + 200 files at the per-file cap would be 100 MiB). Files past it
# are listed `too_large`.
MAX_CONTENT_BYTES = 8 * 1024 * 1024
MAX_BRANCHES = 200
MAX_ERROR = 300
_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_REGULAR = frozenset({"100644", "100755"})
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The fetch talks to the user's remote with the user's credentials, and
# never asks: no terminal (its own session), no ssh or credential-manager
# window, no `ext::` transport.
_FETCH = ("-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "protocol.ext.allow=never")
_NEVER_ASK = {"SSH_ASKPASS_REQUIRE": "never", "GCM_INTERACTIVE": "never"}


class _Refused(Exception):
    """The request can't be answered: a line for a person."""


def fetch(top: Path, remote: str, timeout: float = FETCH_SECONDS) -> bool:
    """`git fetch --prune <remote>` with the user's own credentials and no
    prompt, killed (with ssh) after `timeout`. Whether it worked."""
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with  # killed as a group
            ["git", "-C", str(top), *SAFE, *_FETCH,
             "fetch", "--quiet", "--prune", "--no-recurse-submodules", remote],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**git_env(), **_NEVER_ASK}, start_new_session=True)
    except OSError:
        return False
    try:
        return proc.wait(timeout) == 0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        return False


def valid_branch(cwd: Path, branch: str) -> bool:
    if branch.startswith("-") or len(branch) > MAX_BRANCH:
        return False
    out = git(cwd, "check-ref-format", "--branch", branch)
    return out.returncode == 0 and out.stdout.strip() == branch  # no `@{-1}` expanding


def _commit(top: Path, branch: str, remote: str) -> str:
    """The commit `branch` names, local first, else the remote's; `HEAD` for ""."""
    refs = ["HEAD"]
    if branch:
        refs = [f"refs/heads/{branch}"] + ([f"refs/remotes/{remote}/{branch}"] if remote else [])
    for ref in refs:
        out = git(top, "rev-parse", "--verify", "-q", ref + "^{commit}")
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return ""


def _git_bytes(top: Path, *args: str, stdin: bytes = b"") -> bytes:
    out = subprocess.run(["git", "-C", str(top), *SAFE, *args], input=stdin,
                         capture_output=True, env=git_env(), check=False, timeout=FETCH_SECONDS)
    if out.returncode != 0:
        command = next((a for a in args if not a.startswith("-")), "git")
        raise _Refused(f"couldn't read the project (git {command} failed)")
    return out.stdout


@dataclass(frozen=True)
class _Entry:
    name: str
    file: str
    oid: str
    size: int


def _listing(top: Path, commit: str, directory: str) -> list[_Entry]:
    """The `<stem>.md` blobs right in `directory` (repo-relative, ending in
    `/`) at `commit`: no symlink, submodule or subdirectory. Sorted by name,
    at most `MAX_FILES`."""
    out = _git_bytes(top, "--literal-pathspecs", "ls-tree", "-z", "-l", commit, "--", directory)
    found: dict[str, _Entry] = {}
    for entry in out.decode("utf-8", errors="replace").split("\0"):
        meta, _, file = entry.partition("\t")
        fields = meta.split()
        if len(fields) != 4 or fields[0] not in _REGULAR or fields[1] != "blob":
            continue
        name = file.rpartition("/")[2]
        stem = name.removesuffix(".md")
        if (name.endswith(".md") and _STEM.fullmatch(stem) and file == directory + name
                and _OID.fullmatch(fields[2])):
            size = int(fields[3]) if fields[3].isdigit() else MAX_FILE_BYTES + 1
            found[stem] = _Entry(stem, file, fields[2], size)
    return [found[name] for name in sorted(found)[:MAX_FILES]]


def _blobs(top: Path, oids: list[str]) -> dict[str, bytes]:
    """Each blob's bytes, read in one `git cat-file --batch` (no filters)."""
    if not oids:
        return {}
    data = _git_bytes(top, "cat-file", "--batch", stdin="".join(f"{o}\n" for o in oids).encode())
    blobs: dict[str, bytes] = {}
    at = 0
    while at < len(data):
        end = data.index(b"\n", at)
        header = data[at:end].decode("ascii", errors="replace").split()
        if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
            raise _Refused(f"couldn't read {header[0] if header else 'a file'}")
        size = int(header[2])
        blobs[header[0]] = data[end + 1:end + 1 + size]
        at = end + 1 + size + 1
    return blobs


def manifests(top: Path, commit: str, bottles: str,
              agents: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The manifests in the `bottles` and `agents` directories at `commit`
    (`_listing`), with their text; one over `MAX_FILE_BYTES`, or past
    `MAX_CONTENT_BYTES` in all (bottles first), without it (`too_large`)."""
    listings = [_listing(top, commit, bottles), _listing(top, commit, agents)]
    # Read only what can fit: the JSON text is never shorter than the bytes.
    wanted: dict[str, None] = {}
    room = MAX_CONTENT_BYTES
    for entry in (e for listing in listings for e in listing):
        if entry.size <= min(MAX_FILE_BYTES, room) and entry.oid not in wanted:
            wanted[entry.oid] = None
            room -= entry.size
    blobs = _blobs(top, list(wanted))
    room = MAX_CONTENT_BYTES
    replies: list[list[dict[str, Any]]] = []
    for listing in listings:
        files: list[dict[str, Any]] = []
        for entry in listing:
            blob = blobs.get(entry.oid)
            content = blob.decode("utf-8", errors="replace") if blob is not None else None
            cost = len(json.dumps(content)) if content is not None else 0
            if content is None or cost > room:
                files.append({"name": entry.name, "file": entry.file, "content": None,
                              "too_large": True})
            else:
                room -= cost
                files.append({"name": entry.name, "file": entry.file, "content": content})
        replies.append(files)
    return replies[0], replies[1]


def branches(top: Path, remote: str) -> list[str]:
    """Local branches and `remote`'s, its `HEAD` left out, at most `MAX_BRANCHES`."""
    spaces = ["refs/heads/"] + ([f"refs/remotes/{remote}/"] if remote else [])
    out = git(top, "for-each-ref", "--format=%(refname)", *spaces)
    names: set[str] = set()
    for ref in out.stdout.splitlines():
        for space in spaces:
            if ref.startswith(space):
                name = ref[len(space):]
                if name and not (space != "refs/heads/" and name == "HEAD"):
                    names.add(name)
    return sorted(names)[:MAX_BRANCHES]


def _answer(msg: dict[str, Any], checkouts: CheckoutStore) -> dict[str, Any]:
    repo, path, branch = msg.get("repo"), msg.get("path"), msg.get("branch", "")
    if not isinstance(repo, str) or not isinstance(path, str) or not isinstance(branch, str):
        raise _Refused("project_bottles needs a repo, a path and a branch")
    kept = checkouts.get(path) if path else None
    if kept is None or kept.repo != repo or not kept.reportable():
        raise _Refused(f"this host has no checkout of {repo} at {path}")
    checkout = Path(kept.path)
    if not checkout.is_dir():
        raise _Refused(f"{path} is gone from this host")
    if branch and not valid_branch(checkout, branch):
        raise _Refused(f"{branch!r} isn't a branch name")
    top_out = git(checkout, "rev-parse", "--show-toplevel")
    if top_out.returncode != 0 or not top_out.stdout.strip():
        raise _Refused(f"{path} isn't in a git work tree anymore")
    top = Path(top_out.stdout.strip()).resolve()
    remote = remote_name(top)
    fetched = fetch(top, remote) if remote and not remote.startswith("-") else False
    commit = _commit(top, branch, remote)
    if not commit:
        raise _Refused(f"no branch {branch} in {path}" if branch else f"no commit in {path}")
    rel = checkout.resolve().relative_to(top).as_posix()
    prefix = "" if rel == "." else rel + "/"
    bottles, agents = manifests(top, commit, f"{prefix}.bot-bottle/bottles/",
                                f"{prefix}.bot-bottle/agents/")
    return {"type": "project_bottles", "request_id": msg.get("request_id"), "repo": repo,
            "path": path, "branch": branch, "commit": commit, "fetched": fetched,
            "bottles": bottles, "agents": agents,
            "branches": branches(top, remote)}


def answer(msg: dict[str, Any], checkouts: CheckoutStore) -> dict[str, Any]:
    """The `project_bottles` reply to `msg`, or its `error`."""
    try:
        return _answer(msg, checkouts)
    except (_Refused, OSError, ValueError, subprocess.SubprocessError) as e:
        error = str(e) if isinstance(e, _Refused) else f"couldn't read the project: {e}"
        return {"type": "project_bottles", "request_id": msg.get("request_id"),
                "error": " ".join(error.split())[:MAX_ERROR]}


__all__ = ["FETCH_SECONDS", "MAX_BRANCHES", "MAX_CONTENT_BYTES", "MAX_FILES", "MAX_FILE_BYTES",
           "answer",
           "branches", "fetch", "manifests"]
