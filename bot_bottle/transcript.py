"""Harness transcripts as normalized entries (PRD 0095).

Each provider turns its harness's raw JSONL lines into `Part`s; `entries`
numbers them and ties each to the byte offset of its line in the host copy.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

TranscriptKind = Literal["user", "assistant", "tool_call", "tool_result", "system"]


@dataclass(frozen=True)
class TranscriptEntry:
    """One normalized entry of a harness transcript.

    `raw_offset` is the byte offset of the line it came from in the host copy;
    one line can yield several entries (a reply and its tool calls). `id` and
    `parent` carry a harness's own tree links, for following its active
    branch; "" when it has none. `thinking` marks the model's reasoning (an
    `assistant` entry the view collapses); `meta` marks the harness's own
    bookkeeping (snapshots, token counts, injected context) the view hides
    unless asked."""

    seq: int
    ts: str
    kind: TranscriptKind
    text: str
    raw_offset: int
    tool: str = ""
    error: bool = False
    id: str = ""
    parent: str = ""
    thinking: bool = False
    meta: bool = False

    def to_json(self) -> dict[str, Any]:
        """The wire form: defaults left out."""
        out: dict[str, Any] = {"seq": self.seq, "ts": self.ts, "kind": self.kind,
                               "text": self.text, "raw_offset": self.raw_offset}
        for name in ("tool", "error", "thinking", "meta"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        return out


@dataclass(frozen=True)
class Part:
    """What one raw line says, before numbering."""

    kind: TranscriptKind
    text: str
    tool: str = ""
    error: bool = False
    thinking: bool = False
    meta: bool = False


@dataclass(frozen=True)
class Line:
    """One parsed raw line: its parts, time and tree links."""

    parts: list[Part]
    ts: str = ""
    id: str = ""
    parent: str = ""


def system(text: str, *, meta: bool = False) -> Part:
    return Part("system", text, meta=meta)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def entries(lines: Iterable[str], parse_line: Callable[[dict[str, Any]], Line], *,
            offset: int = 0, seq: int = 0) -> list[TranscriptEntry]:
    """Entries for raw `lines` (each with its newline), numbered from `seq`,
    the first line at byte `offset`. A line that isn't a JSON object is kept
    as a `system` entry with its text."""
    out: list[TranscriptEntry] = []
    for raw_line in lines:
        try:
            raw = json.loads(raw_line)
        except ValueError:
            raw = None
        line = parse_line(raw) if isinstance(raw, dict) else \
            Line([system(raw_line.rstrip("\n"))])
        for part in line.parts:
            out.append(TranscriptEntry(
                seq=seq + len(out), ts=line.ts, kind=part.kind, text=part.text,
                raw_offset=offset, tool=part.tool, error=part.error, id=line.id,
                parent=line.parent, thinking=part.thinking, meta=part.meta))
        offset += len(raw_line.encode("utf-8"))
    return out


def walk_branch(entries_: Sequence[TranscriptEntry],
                also: Callable[[TranscriptEntry, set[str]], bool] | None = None,
                ) -> list[TranscriptEntry]:
    """The entries on the walk up `parent` from the last entry with an id,
    plus those with no id and any `also` admits given the walk's ids, in file
    order."""
    parents: dict[str, str] = {}
    for e in entries_:
        if e.id:
            parents.setdefault(e.id, e.parent)
    path: set[str] = set()
    leaf = next((e.id for e in reversed(entries_) if e.id), "")
    while leaf and leaf not in path:
        path.add(leaf)
        leaf = parents.get(leaf, "")
    return [e for e in entries_
            if not e.id or e.id in path or (also is not None and also(e, path))]


__all__ = ["Line", "Part", "TranscriptEntry", "TranscriptKind", "dumps", "entries", "system",
           "walk_branch"]
