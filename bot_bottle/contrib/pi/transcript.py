"""pi's session files as transcript entries (PRD 0095), per the pinned
`@earendil-works/pi-coding-agent` (0.81.1).

pi writes one JSONL file per session to
`~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`: a `session` header,
then entries linked into a tree by `id`/`parentId`. Going back to an earlier
message (`/tree`) moves the leaf there and appends the new branch to the same
file, so the active branch is the walk up from the last entry. `/fork` starts
a new file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from ...agent_provider import TranscriptEntry, TranscriptKind


def session_dir_name(cwd: str) -> str:
    """The directory pi names for a session's working directory."""
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", cwd)) + "--"


def transcript_glob(guest_home: str, cwd: str) -> str:
    return f"{guest_home.rstrip('/')}/.pi/agent/sessions/{session_dir_name(cwd)}/*.jsonl"


def _text(content: Any) -> str:
    """A message's text: a string, or its text blocks (images as a marker)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            parts.append("[image]")
    return "".join(parts)


def _arguments(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


_Part = tuple[TranscriptKind, str, str, bool]  # kind, text, tool, error


def _system(text: str) -> list[_Part]:
    return [("system", text, "", False)]


def _assistant(message: dict[str, Any]) -> list[_Part]:
    content = message.get("content")
    out: list[_Part] = []
    failed = message.get("stopReason") == "error"
    text = _text(content) or (str(message.get("errorMessage") or "") if failed else "")
    if text or failed:
        out.append(("assistant", text, "", failed))
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            out.append(("tool_call", _arguments(block.get("arguments", {})),
                        str(block.get("name", "")), False))
    return out


def _bash(message: dict[str, Any]) -> list[_Part]:
    """The user's own `!command`."""
    failed = message.get("exitCode") not in (0, None) or bool(message.get("cancelled"))
    return [("tool_result", f"$ {message.get('command', '')}\n{message.get('output', '')}",
             "bash", failed)]


_BY_ROLE: dict[str, Callable[[dict[str, Any]], list[_Part]]] = {
    "user": lambda m: [("user", _text(m.get("content")), "", False)],
    "assistant": _assistant,
    "toolResult": lambda m: [("tool_result", _text(m.get("content")),
                              str(m.get("toolName", "")), bool(m.get("isError")))],
    "bashExecution": _bash,
    "branchSummary": lambda m: _system(str(m.get("summary", ""))),
    "compactionSummary": lambda m: _system(str(m.get("summary", ""))),
}

# Entry types other than `message`, as one line of text.
_DESCRIBE: dict[str, Callable[[dict[str, Any]], str]] = {
    "session": lambda r: f"session started in {r.get('cwd', '')}",
    "model_change": lambda r: f"model: {r.get('provider', '')}/{r.get('modelId', '')}",
    "thinking_level_change": lambda r: f"thinking: {r.get('thinkingLevel', '')}",
    "branch_summary": lambda r: str(r.get("summary", "")),
    "compaction": lambda r: str(r.get("summary", "")),
    "custom_message": lambda r: _text(r.get("content")),
    "session_info": lambda r: f"session name: {r.get('name', '')}",
}


def _message(message: dict[str, Any]) -> list[_Part]:
    handler = _BY_ROLE.get(str(message.get("role")))
    if handler is not None:
        return handler(message)
    return _system(_text(message.get("content")) or json.dumps(message, ensure_ascii=False))


def _entry(raw: dict[str, Any]) -> list[_Part]:
    if raw.get("type") == "message" and isinstance(raw.get("message"), dict):
        return _message(raw["message"])
    # `custom`, `label` and anything newer are kept as they are.
    describe = _DESCRIBE.get(str(raw.get("type")), lambda r: json.dumps(r, ensure_ascii=False))
    return _system(describe(raw))


def parse(lines: Iterable[str], *, offset: int = 0, seq: int = 0) -> list[TranscriptEntry]:
    """Entries for raw session `lines`. A line that isn't a JSON object is
    kept as a `system` entry with its text."""
    entries: list[TranscriptEntry] = []
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            raw = None
        parts: list[_Part]
        if isinstance(raw, dict):
            parts = _entry(raw)
            ts = str(raw.get("timestamp", ""))
            # The header's `id` is the session's, not a tree node's.
            ident = "" if raw.get("type") == "session" else str(raw.get("id") or "")
            parent = str(raw.get("parentId") or "")
        else:
            parts, ts, ident, parent = _system(line.rstrip("\n")), "", "", ""
        for kind, text, tool, error in parts:
            entries.append(TranscriptEntry(seq=seq + len(entries), ts=ts, kind=kind,
                                           text=text, raw_offset=offset, tool=tool,
                                           error=error, id=ident, parent=parent))
        offset += len(line.encode("utf-8"))
    return entries


def active_branch(entries: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """The entries on the branch pi has active: the walk up from the last
    entry's line, plus the lines with no tree id (the header, unparsable
    lines), in file order."""
    parents: dict[str, str] = {}
    for e in entries:
        if e.id:
            parents.setdefault(e.id, e.parent)
    keep: set[str] = set()
    leaf = next((e.id for e in reversed(entries) if e.id), "")
    while leaf and leaf not in keep:
        keep.add(leaf)
        leaf = parents.get(leaf, "")
    return [e for e in entries if not e.id or e.id in keep]


__all__ = ["active_branch", "parse", "session_dir_name", "transcript_glob"]
