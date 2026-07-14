"""Launch flow for the Firecracker backend — temporarily disabled (#385).

The firecracker backend launched a per-bottle companion container (the
egress / git-gate / supervise data plane) alongside each microVM. That
per-bottle-companion architecture was removed in the companion-container removal;
firecracker's replacement — the consolidated per-host gateway — lands in
its own cutover (#354).

Until that lands, launching a firecracker bottle fails closed rather than
silently running the removed path. `prepare` / `status` / cleanup still
work, so `backend status --backend=firecracker` and orphan cleanup are
unaffected.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Generator

from ...log import die
from .bottle import FirecrackerBottle
from .bottle_plan import FirecrackerBottlePlan


@contextmanager
def launch(
    plan: FirecrackerBottlePlan,
    *,
    provision: Callable[[FirecrackerBottlePlan, "FirecrackerBottle"], str | None],
) -> Generator[FirecrackerBottle, None, None]:
    """Fail closed: the firecracker backend is disabled while its
    consolidated (gateway-backed) launch is built in #354."""
    del plan, provision
    die(
        "the firecracker backend is temporarily disabled during the "
        "companion-container removal (#385); its consolidated relaunch "
        "lands in #354. Use --backend=docker for now."
    )
    yield  # unreachable — `die` raises; keeps this a generator/contextmanager
