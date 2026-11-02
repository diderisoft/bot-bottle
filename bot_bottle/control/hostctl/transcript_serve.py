"""Serve a console agent's transcript from its host copy (PRD 0095).

A copy is found by the console agent id recorded beside it, so the console
can read a session's conversation before and after a host controller
restart, and after the session ends, until retention deletes it. Within a
run, all session files are shown in the order they opened so that a `/clear`
(which makes the harness create a new JSONL) shows the complete conversation —
the pre-clear history followed by the post-clear continuation. Each session
file's copies are put together across the bottle's runs: a resumed run copies
only what the agent added to a file the earlier run copied. Entries are
parsed when read, on the harness's active branch, and masked before they
leave the host: token patterns, and every value in the host's secrets file in
its common encodings (`redact_tokens`, the egress scanner's rules). Search
runs over the masked text, so a query can't confirm a secret.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...paths import host_secrets_path
from .transcripts import transcripts_root

if TYPE_CHECKING:
    from ...transcript import TranscriptEntry

PAGE = 50
# Whether an agent is working on a prompt: its harness says from the newest
# lines of its copy (`AgentProvider.working`, reading `WORKING_TAIL` bytes),
# else it counts as working while the copy grew within `WORKING_FOR` seconds.
# An indicator for the console only, never a security decision.
WORKING_FOR = 20.0
WORKING_TAIL = 256 * 1024
MAX_PAGE = 200
# Seqs per session file: the Nth session's entries start at N times this.
SESSION_SEQ_STRIDE = 10_000_000
MAX_MATCHES = 100
SNIPPET = 60  # characters of context either side of a match

# Where each harness keeps its transcripts, for copies whose follow record
# predates naming the harness.
_HARNESS_BY_PATH = (("/.pi/", "pi"), ("/.claude/", "claude"), ("/.codex/", "codex"))


@dataclass(frozen=True)
class Copy:
    path: Path
    harness: str
    session: str
    activation: str


@dataclass(frozen=True)
class _Piece:
    """One host copy: part of a guest file, from guest offset `start`."""

    path: Path
    guest: str
    start: int
    opened: float
    agent_id: str

    @property
    def run(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class Conversation:
    """The guest file an agent's conversation is in, as its copies in the
    order they were made; `copy` is the newest."""

    pieces: tuple[_Piece, ...]
    copy: Copy

    def read(self) -> bytes | None:
        """The guest file's bytes as copied, whole lines only; None when no
        piece can be read."""
        data: bytes | None = None
        for piece in self.pieces:
            try:
                more = piece.path.read_bytes()
            except OSError:
                continue
            if data is None or piece.start <= 0:
                data = more  # the whole file: its first copy, or a rewrite
                continue
            if len(data) > piece.start:
                data = data[:piece.start]
            elif len(data) < piece.start:
                # A gap (a copy that never finished): drop its cut line.
                data = data[:data.rfind(b"\n") + 1]
            data += more
        # Only whole lines: the last may still be arriving.
        return None if data is None else data[:data.rfind(b"\n") + 1]


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _harness(follow: dict[str, Any], guest_path: str) -> str:
    if follow.get("harness"):
        return str(follow["harness"])
    return next((h for marker, h in _HARNESS_BY_PATH if marker in guest_path), "")


def _pieces(root: Path) -> list[_Piece]:
    out = []
    for meta_path in root.glob("*/*/*.meta.json"):
        meta = _read(meta_path)
        copy = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".jsonl")
        try:
            opened = float(meta.get("opened") or 0.0) or copy.stat().st_mtime
        except OSError:
            opened = 0.0
        out.append(_Piece(copy, str(meta.get("guest_path", "")), int(meta.get("start") or 0),
                          opened, str(meta.get("agent_id", ""))))
    return out


def _sessions(agent_id: str) -> list[Conversation]:
    """All sessions for the agent's newest run, oldest-first, each with its
    cross-run pieces stitched together.

    Multiple sessions arise when the harness creates new session files (e.g.
    after `/clear`).  Each guest path is deduplicated to its latest rewrite
    first, so `.rewrite-N` copies don't appear alongside the same guest path.
    Each session's pieces are stitched across bottle runs (a resumed run
    copies only what the agent added to a file the earlier run copied).
    """
    root = transcripts_root()
    if not agent_id or not root.is_dir():
        return []
    pieces = _pieces(root)
    mine = [p for p in pieces if p.agent_id == agent_id]
    if not mine:
        return []
    # Find the newest run (by the most-recently opened piece in it).
    newest_run = max(mine, key=lambda p: p.opened).run
    sessions = {p.run.parent for p in mine}
    # All pieces belonging to the newest run.
    run_mine = [p for p in mine if p.run == newest_run]
    # Per guest path, keep only the latest-opened piece (deduplicates rewrites).
    by_guest: dict[str, _Piece] = {}
    for p in run_mine:
        if p.guest not in by_guest or p.opened > by_guest[p.guest].opened:
            by_guest[p.guest] = p
    # Build a Conversation per guest path, sorted by when that guest path first opened.
    follow = _read(newest_run / "follow.json")
    result = []
    for guest_piece in sorted(by_guest.values(), key=lambda p: p.opened):
        chain = sorted(
            (p for p in pieces if p.guest == guest_piece.guest and p.run.parent in sessions),
            key=lambda p: p.opened,
        )
        result.append(Conversation(
            tuple(chain),
            Copy(chain[-1].path, _harness(follow, guest_piece.guest),
                 newest_run.parent.name, newest_run.name),
        ))
    return result


def find_copies(agent_id: str) -> list[Copy]:
    """All copies for the agent's newest run, oldest-first.

    Multiple copies arise when the harness creates new session files (e.g.
    after `/clear`).  Each guest path is deduplicated to its latest rewrite
    first, so `.rewrite-N` copies don't appear alongside the same guest path.
    The result is ordered by `opened` time so the caller can concatenate
    sessions in chronological order.
    """
    return [s.copy for s in _sessions(agent_id)]


def find_conversation(agent_id: str) -> Conversation | None:
    """The console agent's conversation: the guest file it opened last, with
    every copy of that file its bottle's runs made (a resumed run's starts
    where the earlier one's ended), or None."""
    sessions = _sessions(agent_id)
    return sessions[-1] if sessions else None


def find_copy(agent_id: str) -> Copy | None:
    """The most recently opened copy for the agent's newest run, or None."""
    copies = find_copies(agent_id)
    return copies[-1] if copies else None


def _secret_values() -> dict[str, str]:
    """The host's secrets, keyed so `redact_tokens` masks every one."""
    # pylint: disable=import-outside-toplevel
    from ...secret_source import FileSecrets
    try:
        values = FileSecrets(host_secrets_path())._load()  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-exception-caught  # mask by pattern alone
        return {}
    return {f"SECRET_{i}": v for i, v in enumerate(values.values()) if len(v) >= 8}


def mask(text: str, secrets: dict[str, str]) -> str:
    # pylint: disable=import-outside-toplevel
    from ...gateway.egress.dlp_detectors import redact_tokens
    return redact_tokens(text, env=secrets, sensitive_prefixes=("SECRET_",))


class TranscriptReader:
    """Parsed, masked entries per copy, reparsed only when the copy grows."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[Any, list["TranscriptEntry"]]] = {}
        self._lock = threading.Lock()

    def entries(self, agent_id: str, *, current_only: bool = False,
                ) -> list["TranscriptEntry"] | None:
        """The agent's active, masked entries across all sessions; None without any copy.

        Multiple sessions arise after `/clear`: each session file's active
        branch is concatenated in the order the sessions opened, so the full
        conversation (pre-clear and post-clear) appears as one continuous
        history. An entry's seq is its place in its file plus
        `SESSION_SEQ_STRIDE` per earlier session: increasing, never reused,
        and the same entry's for as long as it's on the branch (seqs have
        gaps; nothing reading them needs them contiguous).

        `current_only` keeps just the newest session (the conversation since
        the last `/clear`). Its entries keep the seqs they have in the full
        history: filtering never renumbers.
        """
        convs = _sessions(agent_id)
        if not convs:
            return None
        combined: list["TranscriptEntry"] = []
        any_read = False
        for index, conv in enumerate(convs):
            if current_only and index != len(convs) - 1:
                continue
            data = conv.read()
            if data is None:
                continue
            any_read = True
            signature = (tuple(p.path for p in conv.pieces), len(data))
            with self._lock:
                cached = self._cache.get(conv.copy.path)
                if cached is not None and cached[0] == signature:
                    per_conv = cached[1]
                else:
                    per_conv = None
            if per_conv is None:
                per_conv = self._parse(conv.copy, data)
                with self._lock:
                    self._cache[conv.copy.path] = (signature, per_conv)
            # Each entry keeps its place in its file (stable as the file
            # grows, and across a branch change), offset per session so the
            # sessions stay in order. Numbering them densely instead meant a
            # seq named a different entry whenever the active branch changed,
            # and the console, scrolled back, lost what it showed.
            base = index * SESSION_SEQ_STRIDE
            combined.extend(replace(e, seq=base + e.seq) for e in per_conv)
        return combined if any_read else None

    @staticmethod
    def _parse(copy: Copy, data: bytes) -> list["TranscriptEntry"]:
        # pylint: disable=import-outside-toplevel
        from ...agent_provider import get_provider
        try:
            provider = get_provider(copy.harness)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"transcripts: can't read {copy.path.name} ({copy.harness or '?'}): {e}")
            return []
        lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
        active = provider.active_entries(provider.parse_transcript(lines))
        secrets = _secret_values()
        return [replace(e, text=mask(e.text, secrets)) for e in active]

    def page(self, agent_id: str, *, before: int | None = None, around: int | None = None,
             limit: int = PAGE, full: bool = False) -> dict[str, Any]:
        """A page of entries: the newest `limit` before seq `before` (or the
        newest overall), or `limit` centred on seq `around`; `first` and
        `last` bound the conversation the page is from.

        That's the current session (since the last `/clear`) unless `full`,
        or a cursor (`before`, `around`) that points into an earlier session
        — a search hit from before a `/clear`, and paging back from it, read
        the full history — or the current session has nothing yet. `scope`
        says which (`current` or `all`); seqs are the same in both."""
        current = self.entries(agent_id, current_only=True) or []
        cursor = around if around is not None else before
        # Nothing in the current session yet (just after `/clear`, or its
        # copy unreadable): the full history rather than a blank view.
        earlier = not current or (cursor is not None and cursor < current[0].seq)
        whole = full or earlier
        all_entries = (self.entries(agent_id) or []) if whole else current
        scope = "all" if whole else "current"
        limit = max(1, min(limit, MAX_PAGE))
        if around is not None:
            at = next((i for i, e in enumerate(all_entries) if e.seq >= around),
                      len(all_entries))
            start = max(0, at - limit // 2)
            chosen = all_entries[start:start + limit]
        else:
            older = [e for e in all_entries if before is None or e.seq < before]
            chosen = older[-limit:]
        return {
            "entries": [e.to_json() for e in chosen],
            "first": all_entries[0].seq if all_entries else None,
            "last": all_entries[-1].seq if all_entries else None,
            "scope": scope,
            # Whether there's a conversation to show at all, copied yet or not.
            "available": bool(all_entries) or self.has_transcript(agent_id),
        }

    def search(self, agent_id: str, query: str, *, kinds: list[str] | None = None,
               limit: int = MAX_MATCHES) -> list[dict[str, Any]]:
        """The newest `limit` entries whose masked text contains `query`
        (case-insensitive), as `seq`, `kind`, `ts` and a snippet around the
        first match. Bookkeeping (`meta`) entries match only when `kinds`
        asks for `meta`."""
        needle = query.strip().casefold()
        if not needle:
            return []
        wanted = set(kinds or ())
        matches: list[dict[str, Any]] = []
        for e in reversed(self.entries(agent_id) or []):
            if e.meta and "meta" not in wanted:
                continue
            if wanted - {"meta"} and e.kind not in wanted:
                continue
            at = e.text.casefold().find(needle)
            if at < 0:
                continue
            start, end = max(0, at - SNIPPET), at + len(needle) + SNIPPET
            snippet = ("…" if start else "") + e.text[start:end].replace("\n", " ") + \
                ("…" if end < len(e.text) else "")
            matches.append({"seq": e.seq, "kind": e.kind, "ts": e.ts, "tool": e.tool,
                            "snippet": snippet})
            if len(matches) >= max(1, min(limit, MAX_MATCHES)):
                break
        return matches

    def last(self, agent_id: str) -> int | None:
        found = self.entries(agent_id)
        return found[-1].seq if found else None

    def has_transcript(self, agent_id: str) -> bool:
        """Whether the agent's harness keeps a transcript the host follows:
        a follow record names it, copied or not yet."""
        root = transcripts_root()
        return root.is_dir() and any(
            _read(p).get("agent_id") == agent_id for p in root.glob("*/*/follow.json"))


def is_working(agent_id: str, now: float) -> bool | None:
    """Whether the agent is working on a prompt (see `WORKING_FOR`), or None
    without a copy to read."""
    # pylint: disable-next=import-outside-toplevel
    from ...agent_provider import get_provider
    copy = find_copy(agent_id)
    if copy is None:
        return None
    try:
        with copy.path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - WORKING_TAIL))
            data = f.read()
        grew = copy.path.stat().st_mtime
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > WORKING_TAIL and lines:
        lines = lines[1:]  # cut by the tail
    try:
        said = get_provider(copy.harness).working(lines)
    except Exception as e:  # pylint: disable=broad-exception-caught  # fall back to growth
        log.debug(f"transcripts: {copy.harness or '?'} can't say if {agent_id} is working: {e}")
        said = None
    return said if said is not None else now - grew < WORKING_FOR


class TranscriptLink:
    """The console link's side: `transcript` and `transcript_search`
    requests, and `transcript_appended` when an agent's conversation grows."""

    REQUESTS = ("transcript", "transcript_search")

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 reader: TranscriptReader | None = None) -> None:
        self.reader = reader or TranscriptReader()
        self._send = send
        # console agent id -> the last entry the console was told of
        self._last: dict[str, int] = {}
        # console agent id -> whether the console was told it's working; per
        # link, so a reconnect tells it each again
        self._working: dict[str, bool] = {}

    async def available(self, agent_id: str) -> bool:
        return await asyncio.to_thread(self.reader.has_transcript, agent_id)

    async def handle(self, msg: dict[str, Any]) -> None:
        agent_id = str(msg.get("agent_id", ""))
        reply: dict[str, Any] = {"request_id": msg.get("request_id"), "agent_id": agent_id}
        if msg.get("type") == "transcript_search":
            query, kinds = msg.get("query"), msg.get("kinds")
            reply["matches"] = await asyncio.to_thread(
                self.reader.search, agent_id, query if isinstance(query, str) else "",
                kinds=[str(k) for k in kinds] if isinstance(kinds, list) else None)
            await self._send({"type": "transcript_matches", **reply})
            return

        def number(name: str) -> int | None:
            value = msg.get(name)
            return value if type(value) is int else None  # pylint: disable=unidiomatic-typecheck

        page = await asyncio.to_thread(self.reader.page, agent_id, before=number("before"),
                                       around=number("around"), limit=number("limit") or PAGE,
                                       full=msg.get("all") is True)
        await self._send({"type": "transcript_entries", **reply, **page})

    async def push(self, agent_ids: Iterable[str]) -> None:
        """Tell the console which of these agents' conversations grew."""
        for agent_id in agent_ids:
            try:
                last = await asyncio.to_thread(self.reader.last, agent_id)
            except Exception as e:  # pylint: disable=broad-exception-caught  # retried next round
                log.debug(f"remote: can't read {agent_id}'s transcript: {e}")
                continue
            if last is not None and last != self._last.get(agent_id):
                self._last[agent_id] = last
                await self._send({"type": "transcript_appended", "agent_id": agent_id,
                                  "last": last})


    async def push_working(self, agent_ids: Iterable[str], now: float | None = None) -> None:
        """Tell the console when one of these agents starts or stops working
        on a prompt (`agent_working`); each agent's state once per link, then
        only its changes."""
        at = time.time() if now is None else now
        for agent_id in agent_ids:
            try:
                working = await asyncio.to_thread(is_working, agent_id, at)
            except Exception as e:  # pylint: disable=broad-exception-caught  # retried next round
                log.debug(f"remote: can't tell if {agent_id} is working: {e}")
                continue
            if working is None or self._working.get(agent_id) == working:
                continue
            self._working[agent_id] = working
            await self._send({"type": "agent_working", "agent_id": agent_id, "working": working})

    def forget(self, agent_id: str) -> None:
        """Drop what the console was told of the agent (it stopped, or was
        presented again), so its state is sent afresh."""
        self._working.pop(agent_id, None)


__all__ = ["Conversation", "Copy", "MAX_PAGE", "PAGE", "TranscriptLink", "TranscriptReader", "WORKING_FOR",
           "find_conversation", "find_copies", "find_copy", "is_working", "mask"]
