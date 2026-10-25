"""The Docker backend's host resources, for `bot-bottle uninstall`: its
containers (bottles' compose projects and the infra), networks and images.
It mounts host directories rather than named volumes, so there are none.
"""

from __future__ import annotations

from ..host_resources import HostResource, is_bot_bottle_image, run_lines, run_ok

# Compose projects, the infra containers and networks are all named so.
_NAME_PREFIX = "bot-bottle-"


def list_resources(*, purge: bool) -> list[HostResource]:
    """Containers first (they hold the networks), then networks and images."""
    found: list[HostResource] = []
    for name in run_lines(["docker", "ps", "--all", "--format", "{{.Names}}"]):
        if name.startswith(_NAME_PREFIX):
            found.append(HostResource(f"container {name}", lambda n=name: run_ok(
                ["docker", "rm", "--force", n])))
    for name in run_lines(["docker", "network", "ls", "--format", "{{.Name}}"]):
        if name.startswith(_NAME_PREFIX):
            found.append(HostResource(f"network {name}", lambda n=name: run_ok(
                ["docker", "network", "rm", n])))
    for ref in run_lines(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]):
        if not ref.endswith(":<none>") and is_bot_bottle_image(ref, purge=purge):
            found.append(HostResource(f"image {ref}", lambda r=ref: run_ok(
                ["docker", "image", "rm", r])))
    return found


__all__ = ["list_resources"]
