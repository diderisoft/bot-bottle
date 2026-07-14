"""Launch flow for the macOS Apple Container backend — disabled (#385).

This backend launched a per-bottle companion container (the egress /
git-gate / supervise data plane) alongside the agent container, with the
agent's proxy env pointed at the companion's host-only IP. That
per-bottle-companion architecture was removed in the de-sidecar cleanup;
the macOS backend will be re-enabled once it grows the consolidated
per-host gateway the docker backend already uses.

Until then, launching a macOS bottle fails closed. `prepare` / `status`
/ cleanup still work.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Generator

from ...log import die
from .bottle import MacosContainerBottle
from .bottle_plan import MacosContainerBottlePlan


@contextmanager
def launch(
    plan: MacosContainerBottlePlan,
    *,
    provision: Callable[[MacosContainerBottlePlan, "MacosContainerBottle"], str | None],
) -> Generator[MacosContainerBottle, None, None]:
    """Fail closed: the macOS backend is disabled until it grows the
    consolidated per-host gateway (the companion-container path it used
    was removed in #385)."""
    del plan, provision
    die(
        "the macos-container backend is temporarily disabled during the "
        "companion-container removal (#385); it will return once it uses "
        "the consolidated gateway. Use --backend=docker for now."
    )
    yield  # unreachable — `die` raises; keeps this a generator/contextmanager
