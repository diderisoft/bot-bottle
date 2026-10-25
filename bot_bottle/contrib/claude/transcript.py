"""Claude Code's session files as transcript entries (PRD 0095), per the
pinned `@anthropic-ai/claude-code` (2.1.172).

Claude writes one JSONL file per session to
`~/.claude/projects/<cwd with every non-alphanumeric as ->/<session uuid>.jsonl`.
Conversation lines (`user`, `assistant`, `system`, `attachment`) link into a
tree by `uuid`/`parentUuid`; an assistant reply is one line per content
block. A tool's result is a `user` line whose parent is its `tool_use` line,
so parallel calls fork the tree for a moment. Rewinding (Esc Esc) appends the
new branch to the same file. Bookkeeping lines (`file-history-snapshot`,
`mode`, `permission-mode`, `ai-title`) carry no `uuid`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from ...transcript import Line, Part, TranscriptEntry, dumps, entries, system, walk_branch


def project_dir_name(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def transcript_glob(guest_home: str, cwd: str) -> str:
    return f"{guest_home.rstrip('/')}/.claude/projects/{project_dir_name(cwd)}/*.jsonl"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(str(block.get("text", "")))
        elif isinstance(block, dict) and block.get("type") == "image":
            out.append("[image]")
    return "".join(out)


# A slash command and its output, as Claude records them in a user line.
_COMMAND = re.compile(r"^<(command-name|command-message|local-command-stdout|"
                      r"local-command-caveat|bash-input|bash-stdout)>")


def _user(raw: dict[str, Any], content: Any, names: dict[str, str]) -> list[Part]:
    if isinstance(content, str):
        meta = bool(raw.get("isMeta")) or bool(_COMMAND.match(content))
        return [Part("system", content, meta=True)] if meta else [Part("user", content)]
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            out.append(Part("tool_result", _text(block.get("content")),
                            tool=names.get(str(block.get("tool_use_id")), ""),
                            error=bool(block.get("is_error"))))
        elif block.get("type") in ("text", "image"):
            text = _text([block])
            out.append(Part("system", text, meta=True) if raw.get("isMeta")
                       else Part("user", text))
    return out


def _assistant(raw: dict[str, Any], content: Any, names: dict[str, str]) -> list[Part]:
    error = bool(raw.get("isApiErrorMessage"))
    if isinstance(content, str):
        return [Part("assistant", content, error=error)]
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(Part("assistant", str(block.get("text", "")), error=error))
        elif kind in ("thinking", "redacted_thinking"):
            out.append(Part("assistant", str(block.get("thinking", "")), thinking=True))
        elif kind in ("tool_use", "server_tool_use"):
            names[str(block.get("id"))] = str(block.get("name", ""))
            out.append(Part("tool_call", dumps(block.get("input", {})),
                            tool=str(block.get("name", ""))))
        else:
            out.append(system(dumps(block)))
    return out


def _system_line(raw: dict[str, Any]) -> list[Part]:
    if raw.get("subtype") == "turn_duration":
        return [system(f"turn took {int(raw.get('durationMs') or 0) / 1000:.1f}s", meta=True)]
    text = str(raw.get("content") or raw.get("subtype") or "")
    return [Part("system", text, error=raw.get("level") == "error")]


_OTHER: dict[str, Callable[[dict[str, Any]], list[Part]]] = {
    "system": _system_line,
    "summary": lambda r: [system(str(r.get("summary", "")))],
    "attachment": lambda r: [system(f"attachment: {(r.get('attachment') or {}).get('type', '')}",
                                    meta=True)],
    "ai-title": lambda r: [system(f"title: {r.get('aiTitle', '')}", meta=True)],
    "file-history-snapshot": lambda r: [system("file history snapshot", meta=True)],
    "mode": lambda r: [system(f"mode: {r.get('mode', '')}", meta=True)],
    "permission-mode": lambda r: [system(f"permission mode: {r.get('permissionMode', '')}",
                                         meta=True)],
}


def parse_line(raw: dict[str, Any], names: dict[str, str] | None = None) -> Line:
    """One line's entries; `names` maps tool-use ids seen so far to their
    tools, so a result can name its tool."""
    names = {} if names is None else names
    kind = str(raw.get("type"))
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    assert isinstance(message, dict)
    if kind == "user":
        parts = _user(raw, message.get("content"), names)
    elif kind == "assistant":
        parts = _assistant(raw, message.get("content"), names)
    else:
        parts = _OTHER.get(kind, lambda r: [system(dumps(r))])(raw)
    if raw.get("isSidechain"):
        parts = [Part(p.kind, p.text, p.tool, p.error, p.thinking, meta=True) for p in parts]
    return Line(parts, ts=str(raw.get("timestamp", "")), id=str(raw.get("uuid") or ""),
                parent=str(raw.get("parentUuid") or ""))


def parse(lines: Iterable[str], *, offset: int = 0, seq: int = 0) -> list[TranscriptEntry]:
    names: dict[str, str] = {}
    return entries(lines, lambda raw: parse_line(raw, names), offset=offset, seq=seq)


def active_branch(entries_: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """The branch Claude has active: the walk up from the last entry, plus
    the results of parallel tool calls on it (each hangs off its own call)."""
    return walk_branch(entries_, lambda e, path: e.kind == "tool_result" and e.parent in path)


__all__ = ["active_branch", "parse", "parse_line", "project_dir_name", "transcript_glob"]
