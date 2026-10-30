"""The macos-container backend's host resources, for `bot-bottle uninstall`:
its infra and agent containers, its networks and DB volume, and its images.
"""

from __future__ import annotations

from ...environment import resource_prefix
from ..host_resources import HostResource, is_bot_bottle_image, run_json, run_ok

_CONTAINER = "container"
# Every container, network and volume this backend creates is named so.
# The active environment's prefix (`bot-bottle-` for live).
_NAME_PREFIX = resource_prefix()


def _name(entry: dict[str, object]) -> str:
    config = entry.get("configuration")
    name = config.get("name") if isinstance(config, dict) else None
    return str(name or entry.get("id") or "")


def list_resources(*, purge: bool) -> list[HostResource]:
    """Containers first (they hold the networks and volume), then networks,
    volumes and images."""
    found: list[HostResource] = []
    for entry in run_json([_CONTAINER, "list", "--all", "--format", "json"]):
        name = str(entry.get("id") or _name(entry))
        if name.startswith(_NAME_PREFIX):
            found.append(HostResource(f"container {name}", lambda n=name: run_ok(
                [_CONTAINER, "delete", "--force", n])))
    for kind in ("network", "volume"):
        for entry in run_json([_CONTAINER, kind, "list", "--format", "json"]):
            name = _name(entry)
            if name.startswith(_NAME_PREFIX):
                found.append(HostResource(f"{kind} {name}", lambda k=kind, n=name: run_ok(
                    [_CONTAINER, k, "delete", n])))
    for entry in run_json([_CONTAINER, "image", "list", "--format", "json"]):
        ref = _name(entry)
        if is_bot_bottle_image(ref, purge=purge):
            found.append(HostResource(f"image {ref}", lambda r=ref: run_ok(
                [_CONTAINER, "image", "delete", r])))
    return found


__all__ = ["list_resources"]
