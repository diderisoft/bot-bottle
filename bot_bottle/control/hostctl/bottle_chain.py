"""A running agent's bottle chain as data, and writing one of its host
bottles, for the console's Bottle tab: one editable form over every file the
agent's bottle is built from.

`chain` lists the files in merge order (the base first, so a later one wins),
each with its kind (`host`, `builtin`, `project`, or `run` for a run's
unsaved bottle, ADR 0017), whether the console may write it (host bottles
only), its digest, and its parsed frontmatter and body;
plus this run's additions. `write` writes one host bottle in that chain, from
data (serialized here) or raw text, if it's still what the console showed:
the new text must parse, hold no secret value, and leave the agent's whole
manifest loading. It's versioned like any edit (PRD 0103) and applies from the
next launch; the running bottle is unaffected.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from ...manifest.schema import DEFAULT_BOTTLE
from .manifest_view import ManifestViewError


class StaleBottleError(ManifestViewError):
    """The bottle changed since the console read it."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _files(identity: str) -> tuple[Any, list[dict[str, Any]]]:
    """The launch record and the chain's files in merge order: each picked
    bottle's parents before it, the picks in launch order, each file once."""
    # pylint: disable=import-outside-toplevel
    from ...manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
    from ...manifest.util import ManifestError
    from ...run_bottle import is_run_path
    from .manifest_view import _index, _load
    meta, _ = _load(identity)
    dirs = _index(meta).bottle_dirs()
    out: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for pick in meta.bottle_names or (DEFAULT_BOTTLE,):
        try:
            _, sources = load_bottle_chain(pick, dirs)
        except ManifestError as e:
            raise ManifestViewError(f"bottle {pick} doesn't load as it is now: {e}") from e
        for source in reversed(sources):  # child before parents, so reversed
            if source.path in seen:
                continue
            seen.add(source.path)
            kind = ("builtin" if BUILTIN_BOTTLES_DIR in source.path.parents
                    else "run" if is_run_path(source.path)
                    else "project" if source.project else "host")
            out.append({"name": f"./{source.path.stem}" if source.project else source.name,
                        "kind": kind, "path": source.path})
    return meta, out


def chain(identity: str, policy: str | None) -> dict[str, Any]:
    """The agent's bottle files with their data, and this run's additions."""
    # pylint: disable=import-outside-toplevel
    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
    from .manifest_elements import elements
    from .manifest_view import _shown
    _, files = _files(identity)
    bottles: list[dict[str, Any]] = []
    for f in files:
        text = f["path"].read_text(encoding="utf-8")
        entry: dict[str, Any] = {"name": f["name"], "kind": f["kind"],
                                 "writable": f["kind"] == "host", "path": _shown(f["path"]),
                                 "digest": _digest(text), "content": text}
        try:
            frontmatter, body = parse_frontmatter(text)
            entry.update({"frontmatter": dict(frontmatter), "body": body})
        except YamlSubsetError as e:
            entry["error"] = str(e)
        bottles.append(entry)
    run = [{k: r[k] for k in ("key", "kind", "name", "detail")}
           for r in elements(identity, policy) if r["source"] == "this run"]
    return {"bottles": bottles, "run": run,
            "targets": [b["name"] for b in bottles if b["writable"]]}


def _secret_names_in(text: str) -> list[str]:
    """The secrets file's entries whose value `text` holds."""
    # pylint: disable-next=import-outside-toplevel,protected-access
    from ...manifest.versions import _secret_values
    from ...paths import host_secrets_path
    return sorted(name for name, value in _secret_values(host_secrets_path()).items()
                  if value in text)


def write(identity: str, bottle: str, digest: str, *,
          frontmatter: object = None, body: object = None,
          content: object = None) -> dict[str, Any]:
    """Write the host bottle `bottle` of the agent's chain, from `frontmatter`
    and `body` or raw `content`, if it's still `digest`. Raises
    ManifestViewError (StaleBottleError when it changed meanwhile)."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import bottle_text
    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
    from ...manifest.util import ManifestError
    from ...manifest.versions import SecretInManifestError, record
    from .manifest_view import _index
    meta, files = _files(identity)
    target = next((f for f in files if f["name"] == bottle), None)
    if target is None:
        raise ManifestViewError(f"{bottle} isn't one of this agent's bottles")
    if target["kind"] != "host":
        raise ManifestViewError(f"{bottle} is a {target['kind']} bottle; it can't be "
                                f"written from here (copy what you need into a host bottle)")
    path: Path = target["path"]
    old = path.read_text(encoding="utf-8")
    if _digest(old) != digest:
        raise StaleBottleError(f"{bottle} changed since it was shown; it's been reloaded")
    if isinstance(content, str):
        text = content if content.endswith("\n") else content + "\n"
    elif isinstance(frontmatter, dict) and isinstance(body, str):
        text = bottle_text({str(k): v for k, v in frontmatter.items()}, body)
    else:
        raise ManifestViewError("send the bottle's frontmatter and body, or its content")
    try:
        parse_frontmatter(text)
    except YamlSubsetError as e:
        raise ManifestViewError(f"it doesn't read as a bottle file: {e}") from e
    leaked = _secret_names_in(text)
    if leaked:
        raise ManifestViewError(f"not saved: it holds the value of {', '.join(leaked)} from the "
                                f"secrets file; refer to secrets by name instead")
    if text == old:
        return {"bottle": bottle, "digest": digest, "content": old, "changed": False}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        # The whole manifest must still load, as the next launch reads it.
        _index(meta).load_for_agent(meta.agent_name, meta.bottle_names or None)
        version = record(f"console: edit {bottle}")
    except (ManifestError, SecretInManifestError) as e:
        path.write_text(old, encoding="utf-8")
        raise ManifestViewError(f"not saved: {e}") from e
    return {"bottle": bottle, "digest": _digest(text), "content": text, "changed": True,
            "version": version}


__all__ = ["StaleBottleError", "chain", "write"]
