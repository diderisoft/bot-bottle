"""Bottles running on the backends: `GET /bottles/live`.

What's actually up, whoever launched it, including bottles from before this
daemon started. Each backend lists its bottles through the `GatewayAttacher`
its reattach flow already uses (macos-container's container scan, firecracker's
live run dirs, docker's labelled compose projects), which also reads each
bottle's source IP: the key the orchestrator's registry matches on.

A backend whose listing fails is reported under `errors` rather than dropped
silently: a partial list would make its bottles look gone, and the
orchestrator reaps registry rows for bottles that look gone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ...contract.hostctl import InfraSettings, Settings

if TYPE_CHECKING:
    from ...backend.gateway_attach import RunningBottle


def _available(name: str) -> bool:
    from ...backend.selection import has_backend  # pylint: disable=import-outside-toplevel
    return has_backend(Settings(), name)


def _running(name: str, infra: InfraSettings | None) -> "list[RunningBottle]":
    from ...backend import get_bottle_backend  # pylint: disable=import-outside-toplevel
    backend = get_bottle_backend(Settings(infra=infra or InfraSettings()), name, prompt=False)
    return backend.infra_service().gateway_attacher().running_bottles()


def _recorded(name: str) -> InfraSettings | None:
    from ...backend.infra_service import recorded_infra_settings  # pylint: disable=import-outside-toplevel
    return recorded_infra_settings(name)


def _names() -> tuple[str, ...]:
    from ...backend.selection import BACKEND_NAMES  # pylint: disable=import-outside-toplevel
    return BACKEND_NAMES


class LiveBottles:
    """Lists the bottles running on each available backend, on the infra
    settings its last bring-up recorded (its networks)."""

    def __init__(
        self, *,
        names: Callable[[], Iterable[str]] = _names,
        available: Callable[[str], bool] = _available,
        running: Callable[[str, InfraSettings | None], "list[RunningBottle]"] = _running,
        recorded: Callable[[str], InfraSettings | None] = _recorded,
    ) -> None:
        self._names = names
        self._available = available
        self._running = running
        self._recorded = recorded

    def known(self, name: str) -> bool:
        return name in tuple(self._names())

    def list(self, backend: str = "") -> dict[str, object]:
        """`{"bottles": [{"backend", "slug", "source_ip"}], "errors": {backend:
        message}}` for `backend`, or every backend when it's "". A bottle
        without an address yet (mid-launch) has `source_ip` ""."""
        bottles: list[dict[str, object]] = []
        errors: dict[str, str] = {}
        for name in self._names():
            if backend and name != backend:
                continue
            try:
                if not self._available(name):
                    continue
                running = self._running(name, self._recorded(name))
            except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
                # Any backend failure (a CLI that errors, a die()) makes its
                # list unknown, not empty.
                errors[name] = getattr(e, "message", "") or str(e) or type(e).__name__
                continue
            bottles.extend({"backend": name, "slug": b.slug, "source_ip": b.source_ip}
                           for b in running)
        return {"bottles": bottles, "errors": errors}


__all__ = ["LiveBottles"]
