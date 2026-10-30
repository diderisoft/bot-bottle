"""The host's agents and bottles for the console's Library (PRD 0109): each
home agent and bottle file under `~/.bot-bottle/`, and the built-in bottles a
home one doesn't shadow, with their text. The console mirrors them read-only
and keeps the last copy for when the host is offline.

Bottle files name secrets, never hold them, so their text is sent as is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...lib import log

# A definition is a short Markdown file; anything bigger is cut, not sent.
MAX_BYTES = 64 * 1024


def _read(path: Path) -> str:
    with path.open("rb") as f:
        data = f.read(MAX_BYTES + 1)
    text = data[:MAX_BYTES].decode("utf-8", errors="replace")
    return text + ("\n… (cut: the file is over 64 KiB)\n" if len(data) > MAX_BYTES else "")


def host_library() -> dict[str, list[dict[str, Any]]]:
    """`{"agents": [...], "bottles": [...]}`, each `{name, content}` (a
    bottle also `builtin`), sorted by name. A file that can't be read is left
    out with a warning."""
    # pylint: disable=import-outside-toplevel
    from ...manifest import ManifestError, ManifestIndex
    from ...manifest.loader import BUILTIN_BOTTLES_DIR, scan_agent_names, scan_bottle_names
    try:
        index = ManifestIndex.resolve(str(Path.home()), missing_ok=True)
    except (ManifestError, OSError) as e:
        log.warn(f"remote: can't list the host's agents and bottles: {e}")
        return {"agents": [], "bottles": []}
    out: dict[str, list[dict[str, Any]]] = {"agents": [], "bottles": []}
    if index.home_md is None:
        return out
    for name, path in sorted(scan_agent_names(index.home_md / "agents").items()):
        try:
            out["agents"].append({"name": name, "content": _read(path)})
        except OSError as e:
            log.warn(f"remote: can't read agent {name}: {e}")
    seen: set[str] = set()
    for directory, project in index.bottle_dirs():
        if project:
            continue
        for name in scan_bottle_names(directory):
            if name in seen:
                continue
            seen.add(name)
            try:
                out["bottles"].append({"name": name, "content": _read(directory / f"{name}.md"),
                                       "builtin": directory == BUILTIN_BOTTLES_DIR})
            except OSError as e:
                log.warn(f"remote: can't read bottle {name}: {e}")
    out["bottles"].sort(key=lambda b: b["name"])
    return out


def resolved_manifest(agent: str, bottles: "list[str] | tuple[str, ...]") -> dict[str, Any]:
    """What launching `agent` in `bottles` (merged in order, 0 the base; none
    means the agent's own `bottle:`) would run with, before it runs: the
    launch page's preview. `{agent, bottles, text, data}`, `text` the merged
    bottle as one bottle file's YAML (as the Manifest tab shows it), or
    `{error}`."""
    # pylint: disable=import-outside-toplevel
    from ... import run_manifest
    from ...manifest import ManifestError, ManifestIndex
    from ...manifest.document import bottle_document
    try:
        index = ManifestIndex.resolve(str(Path.home()))
        manifest = index.load_for_agent(agent, tuple(bottles) or None)
    except ManifestError as e:
        return {"error": str(e)}
    doc = bottle_document(manifest.bottle)
    names = list(bottles) or ([manifest.agent.bottle] if manifest.agent.bottle else [])
    return {"agent": agent, "bottles": names, "text": run_manifest.document_text(doc),
            "data": doc}


__all__ = ["MAX_BYTES", "host_library", "resolved_manifest"]
