"""Copy each session's harness transcript to the host as it's written (PRD 0095).

A follower per session polls the bottle once a second through `exec` (and
once more after the agent exits), reads new bytes by offset from each
transcript file that appeared since the session started (on a resumed home,
also from each file already there, from the size it had: the agent continues
its conversation in it, and the earlier run's copy holds the rest), and
appends them exactly as read to

  ~/.bot-bottle/transcripts/<session>/<activation>/<file>.jsonl   (0600)

The host DB keeps the rest (`TranscriptStore`). A `transcript_files` row per
copy holds the guest path, the offset read so far, the offset it started from
and the console agent id. A `transcript_runs` row per run holds what a
restarted host controller needs to resume: the guest glob, the files that
were there before a fresh session (never copied), or the sizes of those a
resumed one continues, the agent id and the harness. The rows go with the
copies when retention deletes them.

The copy is the agent's account, not evidence: `transcript.captured` records
its running SHA-256 (agent-claimed) every five minutes while it grows and when
it closes. A guest file that shrinks or changes before the offset already
read gets a fresh copy, read from its start, and a `transcript.rewritten`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...paths import bot_bottle_root, host_db_path

# An exec reading the guest's transcripts gives up after this long.
POLL_TIMEOUT_SECONDS = 60.0

if TYPE_CHECKING:
    from ...backend import Bottle
    from .audit_events import HostAudit

POLL_SECONDS = 1.0
# A quiet transcript is polled less and less often, up to this. New bytes, or
# the console typing into the agent (`wake`), bring it back to POLL_SECONDS.
POLL_IDLE_SECONDS = 5.0
CHECKPOINT_SECONDS = 300.0
# Bytes read per file per poll; a longer backlog takes a few polls.
READ_LIMIT = 4 * 1024 * 1024
# Bytes before the offset compared each poll, to notice an edit.
CHECK_BYTES = 64


def transcripts_root() -> Path:
    return bot_bottle_root() / "transcripts"


def copy_dir(session: str, activation: str) -> Path:
    return transcripts_root() / session / activation


def _make_dir(path: Path) -> None:
    """`path` and the transcripts dirs above it, 0700."""
    root = transcripts_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for p in reversed([path, *path.parents]):
        if p.is_relative_to(root) and p != root:
            p.mkdir(exist_ok=True, mode=0o700)


class TranscriptStore(DbStore):
    """Where each session run's copies stand: `transcript_runs` (how to
    resume following it) and `transcript_files` (each copy's offsets). Only
    the host controller reads and writes them."""

    TABLES = {"transcript_runs": MACHINE_SPECIFIC, "transcript_files": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("transcripts", [
            """
            CREATE TABLE transcript_runs (
                session TEXT NOT NULL,
                activation TEXT NOT NULL,
                glob TEXT NOT NULL,
                skip TEXT NOT NULL,
                start TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                harness TEXT NOT NULL,
                closed INTEGER NOT NULL,
                PRIMARY KEY (session, activation)
            )
            """,
            """
            CREATE TABLE transcript_files (
                session TEXT NOT NULL,
                activation TEXT NOT NULL,
                copy TEXT NOT NULL,
                guest_path TEXT NOT NULL,
                offset INTEGER NOT NULL,
                start INTEGER NOT NULL,
                opened REAL NOT NULL,
                agent_id TEXT NOT NULL,
                closed INTEGER NOT NULL,
                PRIMARY KEY (session, activation, copy)
            )
            """,
        ]))
        self.migrate()

    @staticmethod
    def _run(row: Any) -> dict[str, Any]:
        return {"session": row["session"], "activation": row["activation"],
                "glob": row["glob"], "skip": json.loads(row["skip"]),
                "start": json.loads(row["start"]), "agent_id": row["agent_id"],
                "harness": row["harness"], "closed": bool(row["closed"])}

    def put_run(self, session: str, activation: str, *, glob: str, skip: list[str],
                start: dict[str, int], agent_id: str, harness: str, closed: bool) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO transcript_runs VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?)",
                         (session, activation, glob, json.dumps(skip), json.dumps(start),
                          agent_id, harness, int(closed)))
        self._chmod()

    def run(self, session: str, activation: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM transcript_runs WHERE session = ? AND "
                               "activation = ?", (session, activation)).fetchone()
        return self._run(row) if row is not None else None

    def runs(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM transcript_runs "
                                "ORDER BY session, activation").fetchall()
        return [self._run(r) for r in rows]

    def put_file(self, session: str, activation: str, copy: str, *, guest_path: str,
                 offset: int, start: int, opened: float, agent_id: str, closed: bool) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO transcript_files VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (session, activation, copy, guest_path, offset, start, opened,
                          agent_id, int(closed)))
        self._chmod()

    def files(self, session: str = "", activation: str = "") -> list[dict[str, Any]]:
        """The copies of one run, or of every run when none is named."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_files" + (
                    " WHERE session = ? AND activation = ?" if session else "")
                + " ORDER BY session, activation, copy",
                (session, activation) if session else ()).fetchall()
        return [{k: (bool(r[k]) if k == "closed" else r[k]) for k in r.keys()} for r in rows]

    def forget_run(self, session: str, activation: str) -> None:
        """Delete a run's rows, as its copies are deleted."""
        with self._connection() as conn:
            for table in ("transcript_files", "transcript_runs"):
                conn.execute(f"DELETE FROM {table} WHERE session = ? AND activation = ?",
                             (session, activation))


def shell_glob(pattern: str) -> str:
    """`pattern` for the shell: `*` and `?` stay wild, the rest is quoted."""
    return "".join(p if p in ("*", "?") else shlex.quote(p)
                   for p in re.split(r"([*?])", pattern) if p)


@dataclass
class _Copy:
    """One host copy of (part of) one guest file."""

    path: Path
    guest_path: str
    offset: int = 0
    tail: bytes = b""  # the last bytes read, to notice an edit
    sha: Any = field(default_factory=hashlib.sha256)
    size: int = 0
    checkpointed_size: int = -1
    checkpointed_at: float = 0.0
    opened: float = 0.0  # wall clock; the newest file opened is the one shown
    start: int = 0  # the guest offset this copy starts at (a continued file)


def _edited(check: bytes, tail: bytes) -> bool:
    """Whether the guest's bytes before the offset differ from those copied.
    A continued file's copy can hold fewer than `check` (only what was added
    since its start): compare what both have."""
    n = min(len(check), len(tail))
    return check[-n:] != tail[-n:]


@dataclass(frozen=True)
class Listing:
    """What one poll found of one guest file."""

    guest_path: str
    size: int
    check: bytes | None
    data: bytes


def poll_script(glob: str, skip: list[str], known: dict[str, int]) -> str:
    """The `exec` script for one poll: for each matching file not in `skip`,
    `F <size> <path>`, then (for a file read before) `C <base64>` of the
    CHECK_BYTES before its offset, then `D <base64>` of up to READ_LIMIT new
    bytes."""
    cases = [f"    {shlex.quote(p)}) continue;;" for p in skip]
    for path, offset in known.items():
        cases.append(f"    {shlex.quote(path)}) off={offset};;")
    return "\n".join([
        f"for f in {shell_glob(glob)}; do",
        '  [ -f "$f" ] || continue',
        '  case "$f" in',
        *cases,
        "    *) off=0;;",
        "  esac",
        '  size=$(wc -c < "$f" | tr -d " ")',
        '  printf "F %s %s\\n" "$size" "$f"',
        f'  chk=$(( off < {CHECK_BYTES} ? off : {CHECK_BYTES} ))',
        '  if [ "$chk" -gt 0 ] && [ "$size" -ge "$off" ]; then',
        '    printf "C "',
        '    tail -c +$((off - chk + 1)) "$f" | head -c "$chk" | base64 | tr -d "\\n"',
        '    printf "\\n"',
        "  fi",
        '  if [ "$size" -gt "$off" ]; then',
        '    printf "D "',
        f'    tail -c +$((off + 1)) "$f" | head -c {READ_LIMIT} | base64 | tr -d "\\n"',
        '    printf "\\n"',
        "  fi",
        "done",
    ])


def parse_listing(stdout: str) -> list[Listing]:
    found: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "F":
            size, _, path = rest.partition(" ")
            found.append({"guest_path": path, "size": int(size or 0), "check": None,
                          "data": b""})
        elif found and tag == "C":
            found[-1]["check"] = base64.b64decode(rest)
        elif found and tag == "D":
            found[-1]["data"] = base64.b64decode(rest)
    return [Listing(**f) for f in found]


class TranscriptFollower:
    """Follows one session's transcript files into host copies."""

    def __init__(self, *, session: str, activation: str, glob: str, agent_id: str,
                 skip: list[str], audit: "HostAudit", harness: str = "",
                 start: dict[str, int] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session, self.activation = session, activation
        self.glob, self.agent_id, self.skip = glob, agent_id, skip
        # guest path -> the size it had when a resumed session started
        self.resumed_at = dict(start or {})
        self.harness = harness
        self.dir = copy_dir(session, activation)
        self._store = TranscriptStore(host_db_path())
        self._audit = audit
        self._clock = clock
        self._copies: dict[str, _Copy] = {}  # guest path -> its current copy
        self._stop = threading.Event()
        self._wake = threading.Event()  # set by `wake` and `detach`
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._bottle: "Bottle | None" = None

    # --- creating and resuming ---

    @classmethod
    def begin(cls, bottle: "Bottle", *, session: str, activation: str, glob: str,
              agent_id: str, audit: "HostAudit", harness: str = "",
              continuing: bool = False) -> "TranscriptFollower":
        """A follower for a session about to start. Files already there are
        another session's and never copied, unless it's `continuing` (its
        home kept, its harness picking up its last conversation): then each
        is followed from the size it has now."""
        r = bottle.exec(f'for f in {shell_glob(glob)}; do [ -f "$f" ] && '
                        'printf "%s %s\\n" "$(wc -c < "$f" | tr -d " ")" "$f"; done; :',
                        timeout=POLL_TIMEOUT_SECONDS, private=False)
        there: dict[str, int] = {}
        for line in r.stdout.splitlines() if r.returncode == 0 else []:
            size, _, path = line.partition(" ")
            if path and size.isdigit():
                there[path] = int(size)
        follower = cls(session=session, activation=activation, glob=glob, agent_id=agent_id,
                       skip=[] if continuing else list(there), audit=audit, harness=harness,
                       start=there if continuing else None)
        _make_dir(follower.dir)
        follower._save_follow()
        return follower

    @classmethod
    def resume(cls, *, session: str, activation: str,
               audit: "HostAudit") -> "TranscriptFollower | None":
        """The follower a previous host controller left, from its saved
        offsets; None when it followed nothing."""
        store = TranscriptStore(host_db_path())
        follow = store.run(session, activation)
        if follow is None or follow["closed"]:
            return None
        follower = cls(session=session, activation=activation, glob=follow["glob"],
                       agent_id=follow["agent_id"], skip=follow["skip"], audit=audit,
                       harness=follow["harness"], start=follow["start"])
        for meta in store.files(session, activation):
            if meta["closed"]:
                continue
            copy = follower._load(follower.dir / meta["copy"], meta)
            follower._copies[copy.guest_path] = copy
        return follower

    def _load(self, path: Path, meta: dict[str, Any]) -> _Copy:
        copy = _Copy(path, meta["guest_path"], offset=meta["offset"], opened=meta["opened"],
                     start=meta["start"])
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        copy.sha.update(data)
        copy.size = len(data)
        copy.tail = data[-CHECK_BYTES:]
        return copy

    # --- running ---

    def start(self, bottle: "Bottle") -> None:
        self._bottle = bottle
        self._thread = threading.Thread(target=self._run, name=f"transcript-{self.session}",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = POLL_SECONDS
        while True:
            woken = self._wake.wait(interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            grew = self.poll()
            interval = POLL_SECONDS if grew or woken else min(interval * 2, POLL_IDLE_SECONDS)

    def wake(self) -> None:
        """Poll now, and every second again: the agent was just typed into."""
        self._wake.set()

    def detach(self) -> None:
        """Stop polling and leave the copies open, as a host controller that
        exits does; the next one resumes them."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()

    def close(self, bottle: "Bottle | None" = None) -> None:
        """Stop polling, read once more (when there's a bottle to read from),
        and close every copy with a final checkpoint."""
        self.detach()
        bottle = bottle or self._bottle
        if bottle is not None:
            self.poll(bottle)
        with self._lock:
            for copy in self._copies.values():
                self._close_copy(copy)
            self._copies.clear()
        if self._store.run(self.session, self.activation) is not None:
            self._save_follow(closed=True)

    def poll(self, bottle: "Bottle | None" = None) -> bool:
        """Read what's new; whether anything was. Failures are logged and
        retried next poll."""
        bottle = bottle or self._bottle
        if bottle is None:
            return False
        with self._lock:
            # A continued file not copied yet is read from where it was.
            known = {**self.resumed_at, **{p: c.offset for p, c in self._copies.items()}}
            try:
                # Bounded: a poll that never returned held the lock, and
                # `close` (so the bottle's teardown) waited on it for good.
                # Paths and offsets only, so not on stdin (see `Bottle.exec`).
                r = bottle.exec(poll_script(self.glob, self.skip, known),
                                timeout=POLL_TIMEOUT_SECONDS, private=False)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.debug(f"transcript {self.session}: poll failed: {e}")
                return False
            if r.returncode != 0:
                log.debug(f"transcript {self.session}: poll failed: {r.stderr.strip()[:200]}")
                return False
            grew = False
            for listing in parse_listing(r.stdout):
                grew = self._take(listing) or grew
            now = self._clock()
            for copy in self._copies.values():
                if copy.size != copy.checkpointed_size and \
                        now - copy.checkpointed_at >= CHECKPOINT_SECONDS:
                    self._checkpoint(copy, final=False)
        return grew

    def _take(self, listing: Listing) -> bool:
        """Copy `listing`'s new bytes; whether the file changed."""
        copy = self._copies.get(listing.guest_path)
        if copy is None:
            start = self.resumed_at.get(listing.guest_path, 0)
            if start and listing.size == start:
                return False  # a file the agent hasn't added to: no copy yet
            # Shorter than when the session started: not the same file; read
            # it from its start (the next poll, from offset 0).
            copy = self._new_copy(listing.guest_path,
                                  start=start if listing.size >= start else 0)
            if copy.start != start:
                return True
        elif listing.size < copy.offset or (
                listing.check is not None and copy.tail and _edited(listing.check, copy.tail)):
            # Edited after we read it: keep what we have, copy it afresh. The
            # new bytes are from the old offset, so read again from the start.
            self._rewritten(copy, listing)
            return True
        if listing.data:
            self._append(copy, listing.data)
        return bool(listing.data)

    def _copy_path(self, guest_path: str, suffix: str = "") -> Path:
        stem = Path(guest_path).name.removesuffix(".jsonl") or "transcript"
        return self.dir / f"{stem}{suffix}.jsonl"

    def _new_copy(self, guest_path: str, suffix: str = "", start: int = 0) -> _Copy:
        _make_dir(self.dir)
        copy = _Copy(self._copy_path(guest_path, suffix), guest_path, offset=start,
                     opened=time.time(), start=start)
        os.close(os.open(copy.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        copy.checkpointed_size, copy.checkpointed_at = 0, self._clock()
        self._copies[guest_path] = copy
        self._save_meta(copy)
        return copy

    def _append(self, copy: _Copy, data: bytes) -> None:
        fd = os.open(copy.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        copy.sha.update(data)
        copy.size += len(data)
        copy.offset += len(data)
        copy.tail = (copy.tail + data)[-CHECK_BYTES:]
        self._save_meta(copy)

    def _rewritten(self, old: _Copy, listing: Listing) -> _Copy:
        self._close_copy(old)
        n = 1
        while self._copy_path(listing.guest_path, f".rewrite-{n}").exists():
            n += 1
        new = self._new_copy(listing.guest_path, suffix=f".rewrite-{n}")
        self._audit.transcript_rewritten(
            bottle=self.session, activation=self.activation, copy=self._rel(old.path),
            new_copy=self._rel(new.path), guest_path=listing.guest_path,
            offset=old.offset, size=listing.size)
        return new

    def _close_copy(self, copy: _Copy) -> None:
        self._checkpoint(copy, final=True)
        self._save_meta(copy, closed=True)

    def _checkpoint(self, copy: _Copy, *, final: bool) -> None:
        copy.checkpointed_size, copy.checkpointed_at = copy.size, self._clock()
        self._audit.transcript_captured(
            bottle=self.session, activation=self.activation, copy=self._rel(copy.path),
            guest_path=copy.guest_path, size=copy.size,
            sha256=copy.sha.hexdigest(), final=final)

    def _save_meta(self, copy: _Copy, *, closed: bool = False) -> None:
        self._store.put_file(self.session, self.activation, copy.path.name,
                             guest_path=copy.guest_path, offset=copy.offset, start=copy.start,
                             opened=copy.opened, agent_id=self.agent_id, closed=closed)

    def _save_follow(self, *, closed: bool = False) -> None:
        self._store.put_run(self.session, self.activation, glob=self.glob, skip=self.skip,
                            start=self.resumed_at, agent_id=self.agent_id, harness=self.harness,
                            closed=closed)

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(transcripts_root()))


def close_stale(live: set[str], audit: "HostAudit") -> None:
    """Close the copies of sessions that ended while no host controller ran
    (their bottles are gone): the last checkpoint is final."""
    for run in TranscriptStore(host_db_path()).runs():
        session, activation = run["session"], run["activation"]
        if session in live or run["closed"]:
            continue
        follower = TranscriptFollower.resume(session=session, activation=activation, audit=audit)
        if follower is not None:
            follower.close()


__all__ = ["TranscriptFollower", "TranscriptStore", "close_stale", "copy_dir", "parse_listing",
           "poll_script", "shell_glob", "transcripts_root"]
