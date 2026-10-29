"""Bottle versions (PRD 0103): every change to a bottle or agent file is a
version you can list and revert to.

Host files (`~/.bot-bottle/agents/`, `bottles/`) live in a git repo bot-bottle
keeps in `~/.bot-bottle/`, tracking only those two directories, and commits
every change itself: its own writes as it makes them, and hand edits as
"edited by hand" the next time a launch (or `bottles history`) looks. A project
bottle's history is the project's own repo; bot-bottle never commits there.

A commit is refused while a tracked file holds a value from the host secrets
file: these files carry names of secrets, never values.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..lib.log import warn
from ..paths import bot_bottle_root, host_secrets_path

TRACKED = ("agents", "bottles")
GITIGNORE = (
    "# bot-bottle versions agents/ and bottles/ only (PRD 0103).\n"
    "/*\n!/.gitignore\n!/agents/\n!/bottles/\n"
)
_AUTHOR = {"GIT_AUTHOR_NAME": "bot-bottle", "GIT_AUTHOR_EMAIL": "bot-bottle@localhost",
           "GIT_COMMITTER_NAME": "bot-bottle", "GIT_COMMITTER_EMAIL": "bot-bottle@localhost"}
# Shortest secret value checked for: shorter ones match ordinary text.
_MIN_SECRET_LEN = 8


class VersionError(Exception):
    """A version couldn't be read or written."""


class SecretInManifestError(VersionError):
    """A tracked file holds a value from the host secrets file."""


@dataclass(frozen=True)
class Version:
    sha: str
    when: int
    message: str


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # The user's git config and environment stay out of it: no hooks, no
    # signing, no GIT_DIR from a caller's shell.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_AUTHOR)
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, env=env, check=False,
    )
    if check and result.returncode != 0:
        raise VersionError(f"git {args[0]}: {(result.stderr or result.stdout).strip()}")
    return result


@contextmanager
def _locked(root: Path) -> Generator[None, None, None]:
    """One writer at a time: the CLI and the host controller both commit."""
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".versions.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _ensure_repo(root: Path) -> None:
    if (root / ".git").is_dir():
        return
    _git(root, "init", "-q")
    (root / ".gitignore").write_text(GITIGNORE)


def _secret_values(secrets_file: Path) -> Mapping[str, str]:
    # Imported here: secret_source pulls in more than this module needs.
    from ..secret_source import FileSecrets  # pylint: disable=import-outside-toplevel
    if not secrets_file.is_file():
        return {}
    return {name: value for name, value in FileSecrets(secrets_file).items()
            if len(value) >= _MIN_SECRET_LEN}


def _check_staged(root: Path, secrets_file: Path) -> None:
    values = _secret_values(secrets_file)
    if not values:
        return
    added = [line[1:] for line in _git(root, "diff", "--cached", "-U0").stdout.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    found = sorted({name for name, value in values.items()
                    if any(value in line for line in added)})
    if found:
        _git(root, "reset", "-q")
        raise SecretInManifestError(
            f"not versioned: a file in {root} holds the value of {', '.join(found)} "
            f"from the secrets file; refer to secrets by name instead")


def record(message: str, *, root: Path | None = None,
           secrets_file: Path | None = None) -> str:
    """Commit any change under the tracked directories as `message`; the
    current version (the repo's HEAD), or "" when there's nothing yet."""
    root = root or bot_bottle_root()
    with _locked(root):
        _ensure_repo(root)
        _git(root, "add", "-A", "--", ".gitignore",
             *(d for d in TRACKED if (root / d).exists()))
        if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
            _check_staged(root, secrets_file or host_secrets_path())
            _git(root, "commit", "-q", "--no-verify", "-m", message)
        head = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
        return head.stdout.strip()


def record_or_warn(message: str, *, root: Path | None = None) -> str:
    """`record`, but a failure only warns: the write it versions stands."""
    try:
        return record(message, root=root)
    except VersionError as e:
        warn(f"bottle versions: {e}")
        return ""


def record_hand_edits(*, root: Path | None = None) -> str:
    """Version whatever changed since bot-bottle last looked."""
    return record("edited by hand", root=root)


def _repo_of(path: Path, root: Path) -> tuple[Path, bool]:
    """The repo holding `path`, and whether it's the host's own."""
    path = path.resolve()
    host = root.resolve()
    if any(path.is_relative_to(host / d) for d in TRACKED):
        return host, True
    top = _git(path.parent, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise VersionError(f"{path} isn't in a git repo, so it has no history")
    return Path(top.stdout.strip()), False


def _rel(path: Path, repo: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def history(path: Path, *, root: Path | None = None) -> list[Version]:
    """`path`'s versions, newest first."""
    root = root or bot_bottle_root()
    repo, host = _repo_of(path, root)
    if host:
        record_hand_edits(root=root)
    out = _git(repo, "log", "--format=%H%x1f%ct%x1f%s", "--", _rel(path, repo)).stdout
    return [Version(sha, int(when), message)
            for sha, when, message in (line.split("\x1f", 2) for line in out.splitlines())]


def is_dirty(path: Path, *, root: Path | None = None) -> bool:
    """Whether `path` differs from its last version."""
    repo, _ = _repo_of(path, root or bot_bottle_root())
    rel = _rel(path, repo)
    return bool(_git(repo, "status", "--porcelain", "--", rel).stdout.strip())


def _resolve(repo: Path, rel: str, version: str) -> str:
    """The full sha of `version` (any unique prefix), which must touch `rel`."""
    out = _git(repo, "log", "--format=%H", "--", rel).stdout.split()
    matches = [sha for sha in out if sha.startswith(version)] if version else []
    if len(matches) != 1:
        raise VersionError(f"{rel} has no version {version!r}"
                           if not matches else f"{version!r} is ambiguous")
    return matches[0]


def show(path: Path, version: str, *, root: Path | None = None) -> str:
    """What `version` changed in `path`, as a diff."""
    repo, _ = _repo_of(path, root or bot_bottle_root())
    rel = _rel(path, repo)
    return _git(repo, "show", "--format=", _resolve(repo, rel, version), "--", rel).stdout


def revert(path: Path, version: str, *, root: Path | None = None) -> str:
    """Make `path`'s content what it was at `version`, as a new version. For a
    host file, that version's sha; for a project file, "" (it's left for you
    to commit)."""
    root = root or bot_bottle_root()
    repo, host = _repo_of(path, root)
    rel = _rel(path, repo)
    sha = _resolve(repo, rel, version)
    content = _git(repo, "show", f"{sha}:{rel}", check=False)
    if content.returncode != 0:
        raise VersionError(f"{rel} didn't exist at {sha[:9]}")
    if host:
        record_hand_edits(root=root)
    path.write_text(content.stdout)
    return record(f"revert {rel} to {sha[:9]}", root=root) if host else ""


def launch_versions(cwd: str, *, root: Path | None = None) -> dict[str, object]:
    """What a launch records: the host's version (after versioning hand
    edits), and for a project with its own bottles, the project's commit and
    whether its `.bot-bottle/` has uncommitted changes."""
    root = root or bot_bottle_root()
    out: dict[str, object] = {}
    try:
        out["host"] = record_hand_edits(root=root)
    except VersionError as e:
        out["host_error"] = str(e)
    project = Path(cwd) / ".bot-bottle"
    # From the home directory, `./.bot-bottle` is the host root itself.
    if project.is_dir() and project.resolve() != root.resolve():
        top = _git(project, "rev-parse", "--show-toplevel", check=False)
        if top.returncode == 0:
            repo = Path(top.stdout.strip())
            head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
            out["project"] = head.stdout.strip()
            out["project_dirty"] = bool(_git(
                repo, "status", "--porcelain", "--", _rel(project, repo)).stdout.strip())
    return out


__all__ = [
    "SecretInManifestError", "TRACKED", "Version", "VersionError", "history", "is_dirty",
    "launch_versions", "record", "record_hand_edits", "record_or_warn", "revert", "show",
]
