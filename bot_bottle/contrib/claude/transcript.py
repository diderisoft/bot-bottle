"""Claude Code's session files as transcript entries (PRD 0095), per the
pinned `@anthropic-ai/claude-code` (2.1.172).

Claude writes one JSONL file per session to
`~/.claude/projects/<cwd with every non-alphanumeric as ->/<session uuid>.jsonl`.
Conversation lines (`user`, `assistant`, `system`, `attachment`) link into a
tree by `uuid`/`parentUuid`; an assistant reply is one line per content
block. A tool's result is a `user` line whose parent is its `tool_use` line,
so parallel calls fork the tree for a moment. Rewinding (Esc Esc) appends the
new branch to the same file. Compacting writes a `system` `compact_boundary`
line with no `parentUuid` and the line it follows as `logicalParentUuid`.
Bookkeeping lines (`file-history-snapshot`, `mode`, `permission-mode`,
`ai-title`, `last-prompt`, `api-request`, and more with each release) carry
no `uuid`; one of a type not named here is meta, by
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


def _image_url(block: dict[str, Any]) -> str:
    """A data URI or HTTPS URL from an image content block's source."""
    source = block.get("source")
    if not isinstance(source, dict):
        return ""
    if source.get("type") == "url":
        url = str(source.get("url", ""))
        return url if url.startswith(("https://", "http://")) else ""
    if source.get("type") == "base64":
        mt = str(source.get("media_type", "image/png"))
        data = str(source.get("data", ""))
        return f"data:{mt};base64,{data}" if data else ""
    return ""


def _content(content: Any) -> tuple[str, str]:
    """(text, first image_url) from a content array; text-only for strings.
    Images whose source can't be resolved contribute a [image] placeholder."""
    if isinstance(content, str):
        return content, ""
    texts: list[str] = []
    url = ""
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            u = _image_url(block)
            if u and not url:
                url = u
            else:
                texts.append("[image]")
    return "".join(texts), url


# A slash command and its output, as Claude records them in a user line.
_COMMAND = re.compile(r"^<(command-name|command-message|local-command-stdout|"
                      r"local-command-caveat|bash-input|bash-stdout)>")


def _user(raw: dict[str, Any], content: Any, names: dict[str, str]) -> list[Part]:
    if isinstance(content, str):
        meta = bool(raw.get("isMeta")) or bool(_COMMAND.match(content))
        return [Part("system", content, meta=True)] if meta else [Part("user", content)]
    out, inline = [], []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            if inline:
                text, url = _content(inline)
                out.append(Part("system", text, meta=True) if raw.get("isMeta")
                           else Part("user", text, image_url=url))
                inline = []
            text, url = _content(block.get("content"))
            out.append(Part("tool_result", text,
                            tool=names.get(str(block.get("tool_use_id")), ""),
                            error=bool(block.get("is_error")), image_url=url))
        elif block.get("type") in ("text", "image"):
            inline.append(block)
    if inline:
        text, url = _content(inline)
        out.append(Part("system", text, meta=True) if raw.get("isMeta")
                   else Part("user", text, image_url=url))
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
        elif kind == "image":
            url = _image_url(block)
            out.append(Part("assistant", "" if url else "[image]", image_url=url, error=error))
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


def _attachment(raw: dict[str, Any]) -> list[Part]:
    """An `attachment` line: context Claude adds to the turn, meta, except a
    `queued_command` prompt (what the person typed while Claude was busy,
    taken up when its turn allowed), which is their turn like any other."""
    att = raw.get("attachment") if isinstance(raw.get("attachment"), dict) else {}
    assert isinstance(att, dict)
    prompt = att.get("prompt")
    if (att.get("type") == "queued_command" and att.get("commandMode", "prompt") == "prompt"
            and isinstance(prompt, str) and prompt.strip()):
        return [Part("user", prompt)]
    return [system(f"attachment: {att.get('type', '')}", meta=True)]


_OTHER: dict[str, Callable[[dict[str, Any]], list[Part]]] = {
    "system": _system_line,
    "summary": lambda r: [system(str(r.get("summary", "")))],
    "attachment": _attachment,
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
    # A compaction's boundary line starts a new chain (`parentUuid` null) but
    # names where it came from in `logicalParentUuid`: following that keeps
    # the history before it on the active branch.
    parent = raw.get("parentUuid") or raw.get("logicalParentUuid") or ""
    return Line(parts, ts=str(raw.get("timestamp", "")), id=str(raw.get("uuid") or ""),
                parent=str(parent))


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
        elif kind == "attachment" and _attachment(raw)[0].kind == "user":
            state = True  # a queued prompt taken up: Claude is on it
    return state
