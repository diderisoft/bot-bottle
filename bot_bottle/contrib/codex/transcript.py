"""Codex's rollout files as transcript entries (PRD 0095), per the pinned
Codex CLI (0.145.0).

Codex writes one JSONL rollout per session to
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`, each line
`{timestamp, type, payload}`. The conversation is the `response_item` lines
(messages, tool calls and their outputs, reasoning); `event_msg` lines repeat
the messages for the TUI and add bookkeeping (token counts, turn start and
end). Going back to edit a message (Esc Esc) starts a new rollout that
copies the history up to that point, so a file has no branches.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from ...transcript import Line, Part, TranscriptEntry, dumps, entries, system

# User-role messages Codex injects itself, not the user's words.
_INJECTED = ("<environment_context>", "<user_instructions>", "<permissions instructions>",
             "# AGENTS.md instructions", "<turn_aborted>")


def transcript_glob(codex_home: str) -> str:
    return f"{codex_home.rstrip('/')}/sessions/*/*/*/rollout-*.jsonl"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if "text" in block:
            out.append(str(block["text"]))
        elif str(block.get("type", "")).endswith("image"):
            out.append("[image]")
    return "".join(out)


def _message(p: dict[str, Any]) -> list[Part]:
    role, text = p.get("role"), _text(p.get("content"))
    if role == "assistant":
        return [Part("assistant", text)]
    if role == "user" and not text.lstrip().startswith(_INJECTED):
        return [Part("user", text)]
    return [system(text, meta=True)]


def _output(p: dict[str, Any]) -> str:
    output = p.get("output")
    if isinstance(output, dict):  # {content, success}
        return _text(output.get("content"))
    return _text(output)


def _failed(p: dict[str, Any]) -> bool:
    output = p.get("output")
    return isinstance(output, dict) and output.get("success") is False


def _reasoning(p: dict[str, Any]) -> list[Part]:
    summary = "\n".join(_text([s]) for s in p.get("summary") or [] if isinstance(s, dict))
    return [Part("assistant", summary, thinking=True)]


_ITEMS: dict[str, Callable[[dict[str, Any]], list[Part]]] = {
    "message": _message,
    "reasoning": _reasoning,
    "function_call": lambda p: [Part("tool_call", str(p.get("arguments", "")),
                                     tool=str(p.get("name", "")))],
    "custom_tool_call": lambda p: [Part("tool_call", str(p.get("input", "")),
                                        tool=str(p.get("name", "")))],
    "local_shell_call": lambda p: [Part("tool_call", dumps(p.get("action", {})), tool="shell")],
    "web_search_call": lambda p: [Part("tool_call", dumps(p.get("action", {})),
                                       tool="web_search")],
    "function_call_output": lambda p: [Part("tool_result", _output(p), error=_failed(p))],
    "custom_tool_call_output": lambda p: [Part("tool_result", _output(p), error=_failed(p))],
}

# `event_msg`s worth showing; the rest repeat a `response_item` or count tokens.
_EVENTS: dict[str, Callable[[dict[str, Any]], Part]] = {
    "error": lambda p: Part("system", str(p.get("message", "")), error=True),
    "stream_error": lambda p: Part("system", str(p.get("message", "")), error=True),
    "turn_aborted": lambda p: system(f"turn aborted: {p.get('reason', '')}"),
    "context_compacted": lambda p: system("context compacted"),
}


def _tool_names(parts: list[Part], p: dict[str, Any], names: dict[str, str]) -> list[Part]:
    """Name a call's output by its call's tool."""
    call = str(p.get("call_id", ""))
    if p.get("type", "").endswith("_call") and call:
        names[call] = str(p.get("name", "")) or parts[0].tool
    elif p.get("type", "").endswith("_call_output") and call in names:
        return [Part(x.kind, x.text, names[call], x.error, x.thinking, x.meta) for x in parts]
    return parts


def parse_line(raw: dict[str, Any], names: dict[str, str] | None = None) -> Line:
    """One line's entries; `names` maps call ids seen so far to their tools."""
    names = {} if names is None else names
    kind = raw.get("type")
    p = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    assert isinstance(p, dict)
    ts = str(raw.get("timestamp", ""))
    if kind == "response_item":
        handler = _ITEMS.get(str(p.get("type")))
        parts = handler(p) if handler else [system(dumps(p))]
        return Line(_tool_names(parts, p, names), ts=ts)
    if kind == "event_msg":
        event = _EVENTS.get(str(p.get("type")))
        return Line([event(p) if event else system(str(p.get("type", "")), meta=True)], ts=ts)
    if kind == "session_meta":
        return Line([system(f"session started in {p.get('cwd', '')} "
                            f"(codex {p.get('cli_version', '')})", meta=True)], ts=ts)
    if kind == "compacted":
        return Line([system(str(p.get("message", "")) or "context compacted")], ts=ts)
    # `turn_context`, `world_state` and anything newer.
    return Line([system(str(kind), meta=True)], ts=ts)


def parse(lines: Iterable[str], *, offset: int = 0, seq: int = 0) -> list[TranscriptEntry]:
    names: dict[str, str] = {}
    return entries(lines, lambda raw: parse_line(raw, names), offset=offset, seq=seq)


__all__ = ["parse", "parse_line", "transcript_glob"]


def working(lines: Iterable[str]) -> bool | None:
    """Whether Codex is mid-turn, from its newest raw rollout lines: the
    `task_started` event starts a turn; `task_complete` and `turn_aborted`
    end it. None when no line says."""
    state: bool | None = None
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict) or raw.get("type") != "event_msg":
            continue
        event = (raw.get("payload") or {}).get("type") if isinstance(raw.get("payload"), dict) \
            else None
        if event == "task_started":
            state = True
        elif event in ("task_complete", "turn_aborted"):
            state = False
    return state
