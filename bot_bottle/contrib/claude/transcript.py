"""Claude Code's session files as transcript entries (PRD 0095), per the
pinned `@anthropic-ai/claude-code` (2.1.172).

Claude writes one JSONL file per session to
`~/.claude/projects/<cwd with every non-alphanumeric as ->/<session uuid>.jsonl`.
Conversation lines (`user`, `assistant`, `system`, `attachment`) link into a
tree by `uuid`/`parentUuid`; an assistant reply is one line per content
block. A tool's result is a `user` line whose parent is its `tool_use` line,
so parallel calls fork the tree for a moment. Rewinding (Esc Esc) appends the
new branch to the same file. Bookkeeping lines (`file-history-snapshot`,
`mode`, `permission-mode`, `ai-title`, `last-prompt`, `api-request`, and more
with each release) carry no `uuid`; one of a type not named here is meta, by
its type alone.
"""

from __future__ import annotations

import json
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


def _unknown(raw: dict[str, Any]) -> list[Part]:
    """A line of a type not named here: a conversation line (it has a `uuid`)
    shown whole, else bookkeeping, meta and named only."""
    if raw.get("uuid"):
        return [system(dumps(raw))]
    return [system(str(raw.get("type")), meta=True)]


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
        parts = _OTHER.get(kind, _unknown)(raw)
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


def _prompt_text(raw: dict[str, Any]) -> str | None:
    """The person's words in a `user` line, or None for one that isn't a
    prompt (a tool result, injected context, a command, a compaction)."""
    if raw.get("isMeta") or raw.get("isCompactSummary") or raw.get("isSidechain"):
        return None
    content = (raw.get("message") or {}).get("content") if isinstance(raw.get("message"), dict) \
        else None
    if isinstance(content, str):
        return None if _COMMAND.match(content) else content
    texts = [str(b.get("text", "")) for b in content if isinstance(b, dict)
             and b.get("type") == "text"] if isinstance(content, list) else []
    return "".join(texts) if texts else None


def working(lines: Iterable[str]) -> bool | None:
    """Whether Claude is mid-turn, from its newest raw lines: a prompt starts
    a turn, as does a reply that calls a tool; `turn_duration` (written as a
    turn ends), a reply that ends the turn (`end_turn`, `stop_sequence`) and
    an interrupt ("[Request interrupted…]") end it. Tool results, bookkeeping
    and subagents' (sidechain) lines don't change it. None when no line says."""
    state: bool | None = None
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # a cut first line, or a partial last one
        if not isinstance(raw, dict) or raw.get("isSidechain"):
            continue
        kind = raw.get("type")
        if kind == "system" and raw.get("subtype") == "turn_duration":
            state = False
        elif kind == "assistant":
            stop = (raw.get("message") or {}).get("stop_reason") \
                if isinstance(raw.get("message"), dict) else None
            if stop == "tool_use":
                state = True
            elif stop in ("end_turn", "stop_sequence"):
                state = False
        elif kind == "user":
            text = _prompt_text(raw)
            if text is not None:
                state = not text.lstrip().startswith("[Request interrupted")
    return state
