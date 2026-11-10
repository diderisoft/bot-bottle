"""Host-wide claims on the firecracker pool's environment slices.

The TAPs are the host's, so a slice's claim is too: one file per claimed slice
in a directory the network-pool setup creates (`BOT_BOTTLE_FC_SLICES_DIR`,
default `/var/lib/bot-bottle-fc/slices`, mode 2775, owned by the pool's group),
so environments of different users (a developer's `dev`, a CI runner's
`ci-<run>`) never take the same slice.

- A claim is the file `<dir>/<slot>`, JSON naming the environment, the user
  (uid) and that user's environment directory (`registration`). It is
  written to a temp file and `link`ed into place, so it appears whole or not
  at all, under a `flock` on `<dir>/.lock` so a scan-then-claim can't race.
- An environment is identified by (uid, name): two users' `dev`s are two
  environments.
- A claim is stale only when its environment directory is confirmed gone (not
  just unreadable) *and* none of the slice's TAPs is in use. A stale claim is
  replaced; a live one never is.
- `bot-bottle envs rm` releases its environment's claim.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LOCK_FILENAME = ".lock"


class SliceClaimError(RuntimeError):
    """The claims directory is missing or unusable, or no slice is free."""


class NoFreeSliceError(SliceClaimError):
    """Every reserved slice is held by a live claim."""


@dataclass(frozen=True)
class Claim:
    """One claimed slice."""

    slot: int
    env: str
    uid: int
    user: str
    # The claiming user's directory for `env`; its absence (with no TAP in
    # use) makes the claim stale.
    registration: str
    pid: int = 0
    claimed_at: float = 0.0

    def to_json(self) -> dict[str, object]:
        return {"env": self.env, "uid": self.uid, "user": self.user,
                "registration": self.registration, "pid": self.pid,
                "claimed_at": self.claimed_at}

    @property
    def owner(self) -> str:
        return f"{self.user or self.uid}:{self.env}"


def _parse(slot: int, raw: str) -> Claim | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            return None
        return Claim(slot=slot, env=str(data["env"]), uid=int(data["uid"]),
                     user=str(data.get("user", "")),
                     registration=str(data.get("registration", "")),
                     pid=int(data.get("pid", 0)),
                     claimed_at=float(data.get("claimed_at", 0.0)))
    except (ValueError, KeyError, TypeError):
        return None


def read_all(directory: Path) -> dict[int, Claim | None]:
    """Every claim file by slot; None for one that's unreadable or malformed
    (treated as held, never stolen)."""
    out: dict[int, Claim | None] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return out
    for path in entries:
        if not path.name.isdigit():
            continue
        slot = int(path.name)
        try:
            out[slot] = _parse(slot, path.read_text(encoding="utf-8"))
        except OSError:
            out[slot] = None
    return out


def find(directory: Path, env: str, uid: int) -> int | None:
    """The slot (uid, env) holds, or None."""
    for slot, held in sorted(read_all(directory).items()):
        if held is not None and held.env == env and held.uid == uid:
            return slot
    return None


def registration_gone(path: str) -> bool:
    """True only when `path` is confirmed not to exist; unreadable (another
    user's private home) counts as present."""
    if not path:
        return False
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def is_stale(held: Claim, in_use: Callable[[int], bool]) -> bool:
    return registration_gone(held.registration) and not in_use(held.slot)


def _require(directory: Path) -> None:
    if not directory.is_dir():
        raise SliceClaimError(
            f"the firecracker slice claims directory {directory} doesn't exist; the "
            f"network pool setup creates it. Re-run `bot-bottle install "
            f"--backend=firecracker` (on NixOS, `nixos-rebuild switch` with the "
            f"updated module)")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise SliceClaimError(
            f"can't write the firecracker slice claims directory {directory}: it "
            f"belongs to the network pool's group; add this user to it (NixOS: "
            f"services.bot-bottle-firecracker.group) or re-run the pool setup")


@contextmanager
def _locked(directory: Path) -> Generator[None]:
    lock = directory / LOCK_FILENAME
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    except PermissionError:
        fd = os.open(lock, os.O_RDONLY)  # flock works on a read-only fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(directory: Path, entry: Claim) -> bool:
    """Create `<directory>/<slot>` whole, or return False if it exists."""
    tmp = directory / f".{entry.slot}.{os.getpid()}.{time.monotonic_ns()}"
    tmp.write_text(json.dumps(entry.to_json(), sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o664)
        os.link(tmp, directory / str(entry.slot))
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def claim(directory: Path, *, env: str, count: int, uid: int, user: str,
          registration: Path, in_use: Callable[[int], bool]) -> int:
    """(uid, env)'s slot in 1..`count`: the one it holds, else the lowest free
    one (or one whose claim is stale). Raises SliceClaimError when the
    directory is unusable and NoFreeSliceError when every slot is held."""
    _require(directory)
    with _locked(directory):
        claims = read_all(directory)
        for slot, held in sorted(claims.items()):
            if held is not None and held.env == env and held.uid == uid and 1 <= slot <= count:
                return slot
        mine = Claim(slot=0, env=env, uid=uid, user=user, registration=str(registration),
                     pid=os.getpid(), claimed_at=time.time())
        for slot in range(1, count + 1):
            if slot in claims:
                held = claims[slot]
                if held is None or not is_stale(held, in_use):
                    continue
                (directory / str(slot)).unlink(missing_ok=True)
            if _write(directory, dataclasses.replace(mine, slot=slot)):
                return slot
        owners = ", ".join(f"{s}: {c.owner if c else 'unreadable'}"
                           for s, c in sorted(claims.items()) if 1 <= s <= count)
    raise NoFreeSliceError(
        f"environment {env!r} needs a firecracker network-pool slice, but all {count} "
        f"reserved slices are held ({owners}). Remove one with `bot-bottle envs rm "
        f"<name>` as its user, or reserve more: set BOT_BOTTLE_FC_ENV_SLOTS (or the "
        f"NixOS module's `envSlots`) higher and re-run the network pool setup "
        f"(`bot-bottle install --backend=firecracker`)")


def release(directory: Path, *, env: str, uid: int) -> bool:
    """Drop (uid, env)'s claim; False when it held none (or the directory is
    missing)."""
    if not directory.is_dir():
        return False
    with _locked(directory):
        slot = find(directory, env, uid)
        if slot is None:
            return False
        (directory / str(slot)).unlink(missing_ok=True)
        return True


__all__ = ["Claim", "NoFreeSliceError", "SliceClaimError", "claim", "find", "is_stale",
           "read_all", "registration_gone", "release"]
