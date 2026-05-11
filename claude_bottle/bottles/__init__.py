"""Per-platform bottle factories.

A bottle is a running, isolated environment with claude inside. Each
platform exposes a factory (currently only Docker) that owns the
end-to-end lifecycle: image build, container/sidecar launch, file
provisioning, and teardown.

Selection is driven by the CLAUDE_BOTTLE_PLATFORM env var (default
"docker"). Per PRD 0003 the manifest does not carry a platform field;
the host environment picks.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from typing import Callable, Protocol

from ..log import die
from .docker import create_docker_bottle


class Bottle(Protocol):
    """Handle to a running bottle. Yielded by a factory's context manager.

    `exec_claude` runs `claude` inside the bottle and blocks until the
    session ends. `cp_in` copies a host path into the bottle. `close`
    is an idempotent alias for context-manager teardown.
    """

    name: str

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int: ...
    def cp_in(self, host_path: str, container_path: str) -> None: ...
    def close(self) -> None: ...


BottleFactory = Callable[..., AbstractContextManager[Bottle]]


_FACTORIES: dict[str, BottleFactory] = {
    "docker": create_docker_bottle,
}


def get_bottle_factory() -> BottleFactory:
    """Resolve the bottle factory for the active platform. Dies with a
    pointer at the known platforms if CLAUDE_BOTTLE_PLATFORM names an
    unimplemented one."""
    name = os.environ.get("CLAUDE_BOTTLE_PLATFORM", "docker")
    if name not in _FACTORIES:
        known = ", ".join(sorted(_FACTORIES))
        die(f"unknown CLAUDE_BOTTLE_PLATFORM={name!r}; known platforms: {known}")
    return _FACTORIES[name]
