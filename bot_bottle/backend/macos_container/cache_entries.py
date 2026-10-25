"""The macos-container backend's cached images, for `bot-bottle prune`
(`oci_cache_entries` holds the rule)."""

from __future__ import annotations

from datetime import datetime

from ..cache_entries import CacheEntry
from ..host_resources import run_json, run_ok
from ..oci_cache_entries import LocalImage, entries_for

_CONTAINER = "container"
_LIBRARY = "docker.io/library/"


def _normal(ref: str) -> str:
    return ref.removeprefix(_LIBRARY)


def _created(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _images() -> list[LocalImage]:
    in_use = set()
    for container in run_json([_CONTAINER, "list", "--all", "--format", "json"]):
        config = container.get("configuration")
        image = config.get("image") if isinstance(config, dict) else None
        if isinstance(image, dict) and image.get("reference"):
            in_use.add(_normal(str(image["reference"])))
    images = []
    for entry in run_json([_CONTAINER, "image", "list", "--format", "json"]):
        config = entry.get("configuration")
        if not isinstance(config, dict) or not config.get("name"):
            continue
        ref = str(config["name"])
        variants = entry.get("variants")
        size = sum(int(v.get("size") or 0) for v in variants
                   if isinstance(v, dict)) if isinstance(variants, list) else 0
        images.append(LocalImage(ref, size, _created(config.get("creationDate")),
                                 _normal(ref) in in_use))
    return images


def list_entries() -> list[CacheEntry]:
    return entries_for(_images(), lambda ref: run_ok([_CONTAINER, "image", "delete", ref]))


__all__ = ["list_entries"]
