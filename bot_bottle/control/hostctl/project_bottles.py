"""A project's bottles and agents at a branch, for the console (PRD 0131):
the reply to `project_bottles`.

Only for a checkout this host reported, named exactly as reported. The host
fetches, best effort, then reads `.bot-bottle/` at the branch's commit with
`git ls-tree` and `git show`: nothing is checked out, committed or stored,
so the working tree is untouched.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

from ...manifest.versions import git_env
from .checkouts import CheckoutStore, git, remote_name

FETCH_SECONDS = 10.0
MAX_FILES = 200  # each of bottles and agents
MAX_FILE_BYTES = 256 * 1024
MAX_BRANCHES = 200
MAX_ERROR = 300
_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_REGULAR = frozenset({"100644", "100755"})


class _Refused(Exception):
    """The request can't be answered: a line for a person."""


def fetch(top: Path, remote: str, timeout: float = FETCH_SECONDS) -> bool:
    """`git fetch --prune <remote>` with the user's own credentials and no
    prompt, killed (with ssh) after `timeout`. Whether it worked."""
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with  # killed as a group
            ["git", "-C", str(top), "-c", "maintenance.auto=false", "-c", "gc.auto=0",
             "fetch", "--quiet", "--prune", "--no-recurse-submodules", remote],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=git_env(), start_new_session=True)
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


def _valid_branch(cwd: Path, branch: str) -> bool:
    if branch.startswith("-"):
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


def _show(top: Path, commit: str, file: str) -> str:
    out = subprocess.run(["git", "-C", str(top), "show", "--no-textconv", f"{commit}:{file}"],
                         capture_output=True, env=git_env(), check=False, timeout=FETCH_SECONDS)
    if out.returncode != 0:
        raise _Refused(f"couldn't read {file} at {commit[:12]}")
    return out.stdout.decode("utf-8", errors="replace")


def manifests(top: Path, commit: str, directory: str) -> list[dict[str, Any]]:
    """The `<stem>.md` files right in `directory` (repo-relative, ending in
    `/`) at `commit`, sorted by name, at most `MAX_FILES`; one over
    `MAX_FILE_BYTES` without its content."""
    out = subprocess.run(["git", "-C", str(top), "ls-tree", "-z", "-l", commit, "--", directory],
                         capture_output=True, env=git_env(), check=False, timeout=FETCH_SECONDS)
    if out.returncode != 0:
        raise _Refused(f"couldn't list {directory} at {commit[:12]}")
    found: dict[str, tuple[str, int]] = {}
    for entry in out.stdout.decode("utf-8", errors="replace").split("\0"):
        meta, _, file = entry.partition("\t")
        fields = meta.split()
        if len(fields) != 4 or fields[0] not in _REGULAR or fields[1] != "blob":
            continue
        name = file.rpartition("/")[2]
        stem = name.removesuffix(".md")
        if name.endswith(".md") and _STEM.fullmatch(stem) and file == directory + name:
            found[stem] = (file, int(fields[3]) if fields[3].isdigit() else MAX_FILE_BYTES + 1)
    files = []
    for name in sorted(found)[:MAX_FILES]:
        file, size = found[name]
        if size > MAX_FILE_BYTES:
            files.append({"name": name, "file": file, "content": None, "too_large": True})
        else:
            files.append({"name": name, "file": file, "content": _show(top, commit, file)})
    return files


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
    if branch and not _valid_branch(checkout, branch):
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
    return {"type": "project_bottles", "request_id": msg.get("request_id"), "repo": repo,
            "path": path, "branch": branch, "commit": commit, "fetched": fetched,
            "bottles": manifests(top, commit, f"{prefix}.bot-bottle/bottles/"),
            "agents": manifests(top, commit, f"{prefix}.bot-bottle/agents/"),
            "branches": branches(top, remote)}


def answer(msg: dict[str, Any], checkouts: CheckoutStore) -> dict[str, Any]:
    """The `project_bottles` reply to `msg`, or its `error`."""
    try:
        return _answer(msg, checkouts)
    except (_Refused, OSError, ValueError, subprocess.SubprocessError) as e:
        error = str(e) if isinstance(e, _Refused) else f"couldn't read the project: {e}"
        return {"type": "project_bottles", "request_id": msg.get("request_id"),
                "error": " ".join(error.split())[:MAX_ERROR]}


__all__ = ["FETCH_SECONDS", "MAX_BRANCHES", "MAX_FILES", "MAX_FILE_BYTES", "answer",
           "branches", "fetch", "manifests"]
