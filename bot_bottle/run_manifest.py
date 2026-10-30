"""A bottled agent's manifest versions (PRD 0107): its resolved bottle as one
document (`manifest.document`), as it launched and as saved since.

Kept in the bottle's state dir, `manifest/`: `vNNNN.yaml` per version and
`log.json` saying when and why each was written. Version 1 is the bottle the
agent launched with; the console's Save adds one with the run's changes.
`launch.json` is what its bottles were at the latest launch or resume (PRD
0111): each one's fingerprint (`manifest.fingerprint`) and the host's bottle
version then.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bottle_state import bottle_state_dir
from .contract.yaml_subset import parse_frontmatter, serialize_yaml_subset


def _dir(identity: str) -> Path:
    return bottle_state_dir(identity) / "manifest"


def versions(identity: str) -> list[dict[str, Any]]:
    """Each version's `version`, `at` (ISO) and `message`, oldest first."""
    try:
        raw = json.loads((_dir(identity) / "log.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [v for v in raw if isinstance(v, dict) and isinstance(v.get("version"), int)]


def document_text(doc: dict[str, object]) -> str:
    """A manifest document as YAML."""
    return serialize_yaml_subset(doc)


def read(identity: str, version: int) -> dict[str, object]:
    """Version `version`'s document."""
    text = (_dir(identity) / f"v{version:04d}.yaml").read_text(encoding="utf-8")
    return dict(parse_frontmatter(f"---\n{text}---\n")[0])


def latest(identity: str) -> tuple[dict[str, Any], dict[str, object]] | None:
    """The newest version's log entry and document, or None."""
    log = versions(identity)
    if not log:
        return None
    return log[-1], read(identity, int(log[-1]["version"]))


def record(identity: str, doc: dict[str, object], message: str) -> int:
    """Write `doc` as the next version; returns its number."""
    directory = _dir(identity)
    directory.mkdir(parents=True, exist_ok=True)
    log = versions(identity)
    number = (int(log[-1]["version"]) if log else 0) + 1
    (directory / f"v{number:04d}.yaml").write_text(document_text(doc), encoding="utf-8")
    log.append({"version": number, "message": message,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    (directory / "log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    return number


def launch_record(identity: str) -> dict[str, Any]:
    """`launch.json`: `bottles` (name -> fingerprint, in chain order) and the
    host's bottle version (`host`, a commit), or {} if there's none."""
    try:
        raw = json.loads((_dir(identity) / "launch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def record_launch(identity: str, bottle: Any, *, names: tuple[str, ...] = (), cwd: str = "",
                  host_version: str = "") -> None:
    """Version 1 at launch. A resume keeps the history, adding a version only
    if the bottle files changed since. The fingerprints of `names` (the
    agent's bottle chain, resolved from `cwd`) and `host_version` become
    `launch.json`, at every launch and resume. Never raises: the launch goes
    on."""
    # pylint: disable=import-outside-toplevel
    from .manifest.document import bottle_document
    from .manifest.fingerprint import chain_hashes
    from .manifest.index import ManifestIndex
    try:
        doc = bottle_document(bottle)
        last = latest(identity)
        if last is None:
            record(identity, doc, "launched")
        elif document_text(last[1]) != document_text(doc):
            record(identity, doc, "resumed with the bottle files as they are now")
        chain = chain_hashes(ManifestIndex.resolve(cwd or str(Path.home())), names)
        (_dir(identity) / "launch.json").write_text(
            json.dumps({"bottles": chain, "host": host_version}, indent=2), encoding="utf-8")
    except Exception as e:  # pylint: disable=broad-exception-caught  # the launch goes on
        # pylint: disable-next=import-outside-toplevel
        from .lib.log import warn
        warn(f"bottle {identity}: couldn't record its manifest version: {e}")


__all__ = ["document_text", "latest", "launch_record", "read", "record", "record_launch",
           "versions"]
