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

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from ...transcript import Line, Part, TranscriptEntry, dumps, entries, system, walk_branch


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
    return value if isinstance(value, str) else dumps(value)


def _assistant(message: dict[str, Any]) -> list[Part]:
    content = message.get("content")
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    out = [Part("assistant", str(b.get("thinking", "")), thinking=True)
           for b in blocks if b.get("type") == "thinking" and b.get("thinking")]
    failed = message.get("stopReason") == "error"
    text = _text(content) or (str(message.get("errorMessage") or "") if failed else "")
    if text or failed:
        out.append(Part("assistant", text, error=failed))
    out.extend(Part("tool_call", _arguments(b.get("arguments", {})), tool=str(b.get("name", "")))
               for b in blocks if b.get("type") == "toolCall")
    return out


def _bash(message: dict[str, Any]) -> list[Part]:
    """The user's own `!command`."""
    failed = message.get("exitCode") not in (0, None) or bool(message.get("cancelled"))
    return [Part("tool_result", f"$ {message.get('command', '')}\n{message.get('output', '')}",
                 tool="bash", error=failed)]


_BY_ROLE: dict[str, Callable[[dict[str, Any]], list[Part]]] = {
    "user": lambda m: [Part("user", _text(m.get("content")))],
    "assistant": _assistant,
    "toolResult": lambda m: [Part("tool_result", _text(m.get("content")),
                                  tool=str(m.get("toolName", "")), error=bool(m.get("isError")))],
    "bashExecution": _bash,
    "branchSummary": lambda m: [system(str(m.get("summary", "")))],
    "compactionSummary": lambda m: [system(str(m.get("summary", "")))],
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


def _message(message: dict[str, Any]) -> list[Part]:
    handler = _BY_ROLE.get(str(message.get("role")))
    if handler is not None:
        return handler(message)
    return [system(_text(message.get("content")) or dumps(message))]


def parse_line(raw: dict[str, Any]) -> Line:
    if raw.get("type") == "message" and isinstance(raw.get("message"), dict):
        parts = _message(raw["message"])
    else:
        # `custom`, `label` and anything newer are kept as they are.
        parts = [system(_DESCRIBE.get(str(raw.get("type")), dumps)(raw))]
    # The header's `id` is the session's, not a tree node's.
    ident = "" if raw.get("type") == "session" else str(raw.get("id") or "")
    return Line(parts, ts=str(raw.get("timestamp", "")), id=ident,
                parent=str(raw.get("parentId") or ""))


def parse(lines: Iterable[str], *, offset: int = 0, seq: int = 0) -> list[TranscriptEntry]:
    return entries(lines, parse_line, offset=offset, seq=seq)


def active_branch(entries_: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """The branch pi has active: the walk up from the last entry, plus the
    header and unparsable lines."""
    return walk_branch(entries_)


__all__ = ["active_branch", "parse", "parse_line", "session_dir_name", "transcript_glob"]
