"""Bottle versions (PRD 0103): every change to a bottle or agent file is a
version you can list and revert to.

Host files (`~/.bot-bottle/agents/`, `bottles/`) live in a git repo bot-bottle
keeps in `~/.bot-bottle/`, tracking only those two directories, and commits
every change itself: its own writes as it makes them, and hand edits as
"edited by hand" the next time a launch (or `bottles history`) looks, or,
while the library syncs, once they're quiet (PRD 0131). A project
bottle's history is the project's own repo; bot-bottle never commits there.

A commit is refused while a tracked file holds a value from the host secrets
file: these files carry names of secrets, never values.

On a paired host whose library syncs (PRD 0131), the host controller names
the host in the repo's config (`AUTHOR_KEY`, `set_author`), and every commit
here, the CLI's too, carries that name as its author, so the library's
history says which host a change came from.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..lib.log import warn
from .util import ManifestError
from ..paths import bot_bottle_root, host_secrets_path, project_manifest_dir

TRACKED = ("agents", "bottles")
GITIGNORE = (
    "# bot-bottle versions agents/ and bottles/ only (PRD 0103).\n"
    "/*\n!/.gitignore\n!/agents/\n!/bottles/\n"
)
_AUTHOR = {"GIT_AUTHOR_NAME": "bot-bottle", "GIT_AUTHOR_EMAIL": "bot-bottle@localhost",
           "GIT_COMMITTER_NAME": "bot-bottle", "GIT_COMMITTER_EMAIL": "bot-bottle@localhost"}
# The repo's config key naming the author of its commits, while it's set.
AUTHOR_KEY = "botbottle.author"
# Shortest secret value checked for: shorter ones match ordinary text.
_MIN_SECRET_LEN = 8


class VersionError(Exception):
    """A version couldn't be read or written."""


class SecretInManifestError(VersionError, ManifestError):
    """A tracked file holds a value from the host secrets file."""


@dataclass(frozen=True)
class Version:
    sha: str
    when: int
    message: str


def git_env() -> dict[str, str]:
    """The environment bot-bottle runs git in on this repo: no GIT_* from a
    caller's shell (GIT_DIR, GIT_ASKPASS), and never a prompt."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str, check: bool = True,
         author: str = "") -> subprocess.CompletedProcess[str]:
    # The user's git config and environment stay out of it: no hooks, no
    # signing, no GIT_DIR from a caller's shell.
    env = git_env()
    env.update(_AUTHOR)
    if author:
        env["GIT_AUTHOR_NAME"] = author
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


def secret_values(secrets_file: Path | None = None) -> Mapping[str, str]:
    """The host secrets file's values long enough to look for in text, by
    name: what must never be written where a name belongs."""
    return _secret_values(secrets_file or host_secrets_path())


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
            f"from the secrets file; refer to it by name instead (secret:NAME)")


def record(message: str, *, root: Path | None = None,
           secrets_file: Path | None = None) -> str:
    """Commit any change under the tracked directories as `message`; the
    current version (the repo's HEAD), or "" when there's nothing yet."""
    root = root or bot_bottle_root()
    with _locked(root):
        return _commit(root, message, secrets_file or host_secrets_path())


def _commit(root: Path, message: str, secrets_file: Path) -> str:
    """`record`'s commit; the caller holds `_locked(root)`."""
    _ensure_repo(root)
    _git(root, "add", "-A", "--", ".gitignore",
         *(d for d in TRACKED if (root / d).exists()))
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _check_staged(root, secrets_file)
        author = _git(root, "config", "--get", AUTHOR_KEY, check=False).stdout.strip()
        _git(root, "commit", "-q", "--no-verify", "-m", message, author=author)
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return head.stdout.strip()


def set_author(name: str, *, root: Path | None = None) -> None:
    """Author this repo's commits as `name` from now on ("": as bot-bottle,
    as an unpaired host's are)."""
    root = root or bot_bottle_root()
    with _locked(root):
        _ensure_repo(root)
        if name:
            _git(root, "config", AUTHOR_KEY, name)
        else:
            _git(root, "config", "--unset-all", AUTHOR_KEY, check=False)


def pending_edits(*, root: Path | None = None) -> list[str]:
    """What changed under the tracked directories since the last version, as
    `git status --porcelain` lines; [] without a repo."""
    root = root or bot_bottle_root()
    if not (root / ".git").is_dir():
        return []
    out = _git(root, "status", "--porcelain", "-z", "--untracked-files=all", "--",
               *TRACKED, check=False).stdout
    return [entry for entry in out.split("\0") if entry]


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


@contextmanager
def writing(root: Path | None = None) -> Generator[str, None, None]:
    """Hold the lock `record` takes, with hand edits committed first, for a
    caller that moves HEAD itself (the library's fast-forward): no bot-bottle
    write lands meanwhile. Yields HEAD ("" for none). The lock isn't
    re-entrant: don't `record` while holding it."""
    root = root or bot_bottle_root()
    with _locked(root):
        yield _commit(root, "edited by hand", host_secrets_path())


def adopt_builtin_agents(names: Iterable[str], *, root: Path | None = None) -> list[str]:
    """Remove each host agent in `names` that's still, byte for byte, a version
    `bot-bottle setup` wrote, by this repo's history, so the built-in of that
    name takes over and upgrades with bot-bottle (PRD 0131). Anything else
    stays, as yours. The names removed; what they held stays in history."""
    root = root or bot_bottle_root()
    names = [n for n in names if (root / "agents" / f"{n}.md").is_file()]
    if not names or not (root / ".git").is_dir():
        return []
    secrets_file = host_secrets_path()
    gone = []
    # Locked throughout, so no bot-bottle write lands between the check and
    # the removal; hand edits are committed first, so nothing unversioned goes.
    with _locked(root):
        _commit(root, "edited by hand", secrets_file)
        for name in names:
            if _as_setup_wrote(root, name):
                (root / "agents" / f"{name}.md").unlink()
                gone.append(name)
        if gone:
            _commit(root, "built in now: " + ", ".join(f"agents/{n}" for n in gone),
                    secrets_file)
    return gone


def _as_setup_wrote(root: Path, name: str) -> bool:
    """Whether `agents/<name>.md` is exactly what a `setup: write
    agents/<name>` commit holds."""
    rel = f"agents/{name}.md"
    current = _git(root, "hash-object", "--", rel, check=False).stdout.strip()
    if not current:
        return False
    for line in _git(root, "log", "--format=%H %s", "--", rel,
                     check=False).stdout.splitlines():
        sha, _, subject = line.partition(" ")
        if subject == f"setup: write agents/{name}" and _git(
                root, "rev-parse", "-q", "--verify", f"{sha}:{rel}",
                check=False).stdout.strip() == current:
            return True
    return False


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


def committed_at(sha: str, *, root: Path | None = None) -> int:
    """When the host's version `sha` was recorded (Unix time), or 0 if it
    isn't one."""
    root = root or bot_bottle_root()
    out = _git(root, "show", "-s", "--format=%ct", sha, check=False)
    return int(out.stdout.strip() or 0) if out.returncode == 0 else 0


def launch_versions(cwd: str, *, root: Path | None = None,
                    host: str = "") -> dict[str, object]:
    """What a launch records: the host's version (after versioning hand
    edits), and for a project with its own bottles, the project's commit and
    whether its `.bot-bottle/` has uncommitted changes. `host`: the library
    commit a console launch names, recorded as is, the repo left alone."""
    root = root or bot_bottle_root()
    out: dict[str, object] = {}
    try:
        out["host"] = host or record_hand_edits(root=root)
    except VersionError as e:
        out["host_error"] = str(e)
    project = project_manifest_dir(cwd, root=root)
    if project is not None:
        top = _git(project, "rev-parse", "--show-toplevel", check=False)
        if top.returncode == 0:
            repo = Path(top.stdout.strip())
            head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
            out["project"] = head.stdout.strip()
            out["project_dirty"] = bool(_git(
                repo, "status", "--porcelain", "--", _rel(project, repo)).stdout.strip())
    return out


__all__ = [
    "AUTHOR_KEY", "SecretInManifestError", "TRACKED", "Version", "VersionError",
    "adopt_builtin_agents", "committed_at", "git_env", "history",
    "is_dirty",
    "launch_versions", "pending_edits", "record", "record_hand_edits", "record_or_warn",
    "revert", "secret_values", "set_author", "show", "writing",
]
