"""The Docker backend's cached images, for `bot-bottle prune`
(`oci_cache_entries` holds the rule)."""

from __future__ import annotations

import json
from datetime import datetime

from ..cache_entries import CacheEntry
from ..host_resources import run_lines, run_ok
from ..oci_cache_entries import LocalImage, entries_for


def _created(value: str) -> float:
    # `2026-10-24 21:51:29 -0400 EDT`: drop the zone name.
    try:
        return datetime.strptime(" ".join(value.split()[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return 0.0


def _size(value: str) -> int:
    units = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    for unit in sorted(units, key=len, reverse=True):
        if value.endswith(unit):
            try:
                return int(float(value[: -len(unit)]) * units[unit])
            except ValueError:
                return 0
    return 0


def _images() -> list[LocalImage]:
    in_use = set(run_lines(["docker", "ps", "--all", "--format", "{{.Image}}"]))
    images = []
    for line in run_lines(["docker", "image", "ls", "--format", "{{json .}}"]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        repo, tag = str(row.get("Repository", "")), str(row.get("Tag", ""))
        if not repo or tag in ("", "<none>"):
            continue
        ref = f"{repo}:{tag}"
        images.append(LocalImage(ref, _size(str(row.get("Size", ""))),
                                 _created(str(row.get("CreatedAt", ""))),
                                 ref in in_use or str(row.get("ID", "")) in in_use))
    return images


def list_entries() -> list[CacheEntry]:
    return entries_for(_images(), lambda ref: run_ok(["docker", "image", "rm", ref]))


__all__ = ["list_entries"]
