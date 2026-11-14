"""An `ssh` forge's keys (ADR 0023): key files this host holds for a plain
SSH git server, kept by reference in its connection (`forge_connections`,
`forge_keys`): the path and its fingerprint, never a copy.

A key is checked as ssh would: the file must be there, a regular file of
this user's that no one else can read or write, and a private key without
a passphrase (git-gate can't type one). Its fingerprint is OpenSSH's
`SHA256:<base64>` of the public key ssh-keygen derives from the private one,
never of a `.pub` beside it, which may be stale. A key rotated in place is
used: the launch that finds the new fingerprint logs it once and records
it, so `forge key list` shows it.

What leaves this module for a launch's error, the log or the console names
the grant and the fingerprint, never the key's path: the path is this
machine's, and launch errors reach the console and the agent.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import os
import stat
import subprocess
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from .lib.log import warn

if TYPE_CHECKING:
    from .manifest.bottle import ManifestBottle
    from .manifest.git import ManifestGitEntry

FINGERPRINT_SECONDS = 10


class KeyFileError(Exception):
    """A key file that can't be used. The message names the file (for the
    operator, on this host); `reason` says why without it, for anything that
    leaves the host. `missing`: the file isn't there."""

    def __init__(self, message: str, reason: str = "", *, missing: bool = False) -> None:
        super().__init__(message)
        self.reason = reason or "can't be used"
        self.missing = missing


def public_fingerprint(line: str) -> str:
    """`SHA256:<base64>` of the public key `<type> <base64> [comment]`, as
    OpenSSH shows it. ValueError when it isn't one."""
    fields = line.split()
    if len(fields) < 2:
        raise ValueError("no public key")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("not base64") from e
    # The blob starts with its own type, length-prefixed.
    size = int.from_bytes(blob[:4], "big")
    if blob[4:4 + size] != fields[0].encode():
        raise ValueError("its type doesn't match")
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def fingerprint(path: str) -> str:
    """`SHA256:<base64>` of the private key at `path`. KeyFileError when it
    isn't an unencrypted private key ssh-keygen can read. ssh-keygen runs in
    a session of its own with an empty passphrase, so it never asks."""
    try:
        done = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", path],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              check=False, timeout=FINGERPRINT_SECONDS, start_new_session=True)
    except FileNotFoundError as e:
        raise KeyFileError("ssh-keygen isn't installed on this host",
                           "can't be checked: ssh-keygen isn't installed on this host") from e
    except subprocess.TimeoutExpired as e:
        raise KeyFileError(f"ssh-keygen didn't read {path} in time",
                           "wasn't read in time") from e
    if done.returncode != 0:
        said = (done.stderr.strip() or "no public key").splitlines()[-1]
        if "passphrase" in said:
            raise KeyFileError(f"{path} is protected by a passphrase, which git-gate can't "
                               f"type; use a key without one",
                               "is protected by a passphrase, which git-gate can't type")
        raise KeyFileError(f"{path} isn't a private key ssh-keygen can read ({said})",
                           "isn't a private key ssh-keygen can read")
    try:
        return public_fingerprint(done.stdout.strip())
    except ValueError as e:
        raise KeyFileError(f"{path} isn't a private key ssh-keygen can read ({e})",
                           "isn't a private key ssh-keygen can read") from e


# The fingerprint last read per path, with the file's stat when it was read:
# listings and every load of a project bottle ask again, ssh-keygen only
# when the file changed.
_SEEN: dict[str, tuple[tuple[int, ...], str]] = {}
_SEEN_LOCK = threading.Lock()


def check_file(path: str) -> str:
    """The fingerprint of the key file at `path` (followed if it's a link),
    once it's a regular file of this user's that only its owner can read or
    write. KeyFileError saying what's wrong."""
    try:
        st = os.stat(path)
    except FileNotFoundError as e:
        raise KeyFileError(f"{path} isn't there", "isn't there", missing=True) from e
    except OSError as e:
        raise KeyFileError(f"{path} can't be read: {e.strerror}",
                           f"can't be read: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise KeyFileError(f"{path} isn't a file", "isn't a regular file")
    if st.st_mode & 0o077:
        mode = f"{stat.S_IMODE(st.st_mode):04o}"
        raise KeyFileError(f"{path} is readable by others (mode {mode}); ssh refuses such a "
                           f"key: `chmod 600 {path}`",
                           f"is open to group or others (mode {mode}), and ssh refuses such "
                           f"a key")
    euid = os.geteuid()
    if euid not in (0, st.st_uid):
        raise KeyFileError(f"{path} is owned by another user (uid {st.st_uid}); a key this "
                           f"host uses must be its own user's",
                           "is owned by another user")
    seen = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    with _SEEN_LOCK:
        cached = _SEEN.get(path)
    if cached is not None and cached[0] == seen:
        return cached[1]
    found = fingerprint(path)
    with _SEEN_LOCK:
        _SEEN[path] = (seen, found)
    return found


def key_path(path: str, cwd: str) -> str:
    """`path` as the absolute path a key is kept under (`~` expanded)."""
    return str((Path(cwd) / Path(path).expanduser()).absolute())


def _grant(entry: "ManifestGitEntry") -> tuple[str, str]:
    return entry.UpstreamHost, entry.UpstreamPath.removesuffix(".git")


def check_launch(entries: "Iterable[ManifestGitEntry]") -> None:
    """Check each repo in `entries` (a bottle's `ManifestGitEntry`s) reached
    with an `ssh` forge's key, before a launch uses it. KeyFileError naming
    the grant and the fix, not the file, when it's missing or unusable. A
    key whose fingerprint changed since it was recorded is used: logged,
    and recorded, so the next launch says nothing."""
    # pylint: disable-next=import-outside-toplevel
    from . import forge_connections
    changed: set[str] = set()
    for entry in entries:
        key = entry.Key
        if key.provider != "ssh":
            continue
        forge, repo = _grant(entry)
        try:
            now = check_file(key.path)
        except KeyFileError as e:
            raise KeyFileError(
                f"the key for {forge}/{repo} can't be used: its file {e.reason}. "
                f"`bot-bottle forge key list {forge}` on this host shows it; fix it, or add "
                f"another with `bot-bottle forge key add {forge} {repo} <path>"
                f"{'' if key.read_only else ' --write'}`", e.reason, missing=e.missing) from e
        if now != key.fingerprint and key.path not in changed:
            changed.add(key.path)
            warn(f"the key for {forge}/{repo} changed since it was added "
                 f"({key.fingerprint or '?'} is now {now}); using it")
            try:
                forge_connections.refingerprint(key.path, now)
            except forge_connections.ForgeConnectionError as e:
                warn(f"couldn't record the key's new fingerprint: {e}")


def refreshed(bottle: "ManifestBottle") -> "ManifestBottle":
    """`bottle` with each `ssh` forge key's fingerprint as its file has it
    now, where that differs from the recorded one: what a launch will use,
    for trust to ask about. A file that can't be used is left as recorded
    (the launch fails on it)."""
    if not any(e.Key.provider == "ssh" for e in bottle.git):
        return bottle
    entries = []
    for entry in bottle.git:
        if entry.Key.provider == "ssh":
            try:
                now = check_file(entry.Key.path)
            except KeyFileError:
                now = entry.Key.fingerprint
            if now != entry.Key.fingerprint:
                entry = dataclasses.replace(
                    entry, Key=dataclasses.replace(entry.Key, fingerprint=now))
        entries.append(entry)
    return dataclasses.replace(bottle, git=tuple(entries))


__all__ = ["FINGERPRINT_SECONDS", "KeyFileError", "check_file", "check_launch", "fingerprint",
           "key_path", "public_fingerprint", "refreshed"]
