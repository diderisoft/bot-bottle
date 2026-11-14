"""An `ssh` forge's keys (ADR 0023): key files this host holds for a plain
SSH git server, kept by reference in its connection (`forge_connections`,
`forge_keys`): the path and its fingerprint, never a copy.

A key is checked as ssh would: the file must be there and readable by its
owner alone. Its fingerprint is `ssh-keygen -lf`'s. A key rotated in place
is used: the launch that finds the new fingerprint logs it once and records
it, so `forge key list` shows it.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .lib.log import warn

if TYPE_CHECKING:
    from .manifest.git import ManifestGitEntry

FINGERPRINT_SECONDS = 10


class KeyFileError(Exception):
    """A key file that can't be used; the message says why and the fix."""


def fingerprint(path: str) -> str:
    """`SHA256:<base64>` of the key at `path`, as `ssh-keygen -lf` shows it.
    KeyFileError when it isn't a key ssh-keygen can read."""
    try:
        done = subprocess.run(["ssh-keygen", "-l", "-f", path], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, check=False,
                              timeout=FINGERPRINT_SECONDS)
    except FileNotFoundError as e:
        raise KeyFileError("ssh-keygen isn't installed on this host") from e
    except subprocess.TimeoutExpired as e:
        raise KeyFileError(f"ssh-keygen didn't read {path} in time") from e
    fields = done.stdout.split()
    if done.returncode != 0 or len(fields) < 2 or ":" not in fields[1]:
        raise KeyFileError(f"{path} isn't a key ssh-keygen can read"
                           f" ({(done.stderr.strip() or 'no fingerprint').splitlines()[0]})")
    return fields[1]


def check_file(path: str) -> str:
    """The fingerprint of the key file at `path`, once it's a regular file
    only its owner can read or write. KeyFileError saying what's wrong."""
    try:
        st = os.stat(path)
    except FileNotFoundError as e:
        raise KeyFileError(f"{path} isn't there") from e
    except OSError as e:
        raise KeyFileError(f"{path} can't be read: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise KeyFileError(f"{path} isn't a file")
    if st.st_mode & 0o077:
        raise KeyFileError(f"{path} is readable by others (mode {stat.S_IMODE(st.st_mode):04o}); "
                           f"ssh refuses such a key: `chmod 600 {path}`")
    return fingerprint(path)


def key_path(path: str, cwd: str) -> str:
    """`path` as the absolute path a key is kept under (`~` expanded)."""
    return str((Path(cwd) / Path(path).expanduser()).absolute())


def check_launch(entries: "Iterable[ManifestGitEntry]") -> None:
    """Check each repo in `entries` (a bottle's `ManifestGitEntry`s) reached
    with an `ssh` forge's key, before a launch uses it. KeyFileError naming
    the grant and the fix when a file is missing or unusable. A key whose
    fingerprint changed since it was recorded is used: logged, and recorded,
    so the next launch says nothing."""
    # pylint: disable-next=import-outside-toplevel
    from . import forge_connections
    changed: set[str] = set()
    for entry in entries:
        key = entry.Key
        if key.provider != "ssh":
            continue
        path = key.path
        repo = entry.UpstreamPath.removesuffix(".git")
        grant = f"{entry.UpstreamHost}/{repo}"
        try:
            now = check_file(path)
        except KeyFileError as e:
            raise KeyFileError(
                f"the key for {grant} can't be used: {e}. Put it back, or add another with "
                f"`bot-bottle forge key add {entry.UpstreamHost} {repo} <path>"
                f"{'' if key.read_only else ' --write'}`") from e
        was = key.fingerprint
        if now != was and path not in changed:
            changed.add(path)
            warn(f"the key {path} for {grant} changed since it was added ({was or '?'} is "
                 f"now {now}); using it")
            try:
                forge_connections.refingerprint(path, now)
            except forge_connections.ForgeConnectionError as e:
                warn(f"couldn't record the key's new fingerprint: {e}")


__all__ = ["FINGERPRINT_SECONDS", "KeyFileError", "check_file", "check_launch", "fingerprint",
           "key_path"]
