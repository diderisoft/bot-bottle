"""The per-host infra service contract (PRD 0070).

`InfraService` is the backend-neutral composer of the per-host **pair**: the
`Orchestrator` (control plane) + `Gateway` (data plane) services, brought up as
an idempotent per-host singleton. One concrete impl per backend
(`backend/*/infra.py`), mirroring how `Orchestrator` and `Gateway` each have a
per-backend impl.

The two backend-specific accessors (`orchestrator()` / `gateway()`) are the
source of truth for how to reach each plane; the convenience `url()` /
`is_healthy()` just delegate to the orchestrator.
"""

from __future__ import annotations

import abc
import fcntl
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ..control.gateway import Gateway
from ..lib.log import die
from ..control.lifecycle import DEFAULT_STARTUP_TIMEOUT_SECONDS, Orchestrator
from ..paths import bot_bottle_root
from .gateway_attach import (
    AttachPolicy,
    GatewayAttachError,
    GatewayAttacher,
    reattach_running_bottles,
)

# How many times this process holds each lock path.
_HELD: dict[Path, int] = {}


@contextmanager
def _reentrant_flock(path: Path) -> Generator[None]:
    """An exclusive flock on `path` that the holding process may re-enter
    (a second flock on a new handle would deadlock against the first)."""
    if _HELD.get(path):
        _HELD[path] += 1
        try:
            yield
        finally:
            _HELD[path] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _HELD[path] = 1
        try:
            yield
        finally:
            del _HELD[path]
            fcntl.flock(handle, fcntl.LOCK_UN)


class InfraService(abc.ABC):
    """Compose + bring up the per-host orchestrator + gateway pair.

    Backend-neutral: docker/macOS run the pair as two containers, firecracker as
    two microVMs. Callers read reach-info off `orchestrator()` / `gateway()`.

    `ensure_running` is the same everywhere: a backend's `_bring_up` brings the
    pair up and reports whether either plane was (re)started, and only then are
    running bottles reattached (PRD 0081). Backends never decide that."""

    # Names this backend's "reattach pending" marker.
    name: str = "infra"

    @abc.abstractmethod
    def orchestrator(self) -> Orchestrator:
        """The control-plane service. Cheap to reconstruct."""

    @abc.abstractmethod
    def gateway(self) -> Gateway:
        """The data-plane service. Cheap to reconstruct."""

    def ensure_running(
        self,
        *,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        attach_policy: AttachPolicy = AttachPolicy.STRICT,
    ) -> str:
        """Bring the orchestrator + gateway pair up (idempotent per-host
        singleton — a healthy, current pair is left running) and, when either
        plane was (re)started, reattach every running bottle to it. Returns
        the host control-plane URL. Raises `OrchestratorStartError` on
        control-plane startup timeout.

        The marker is written before anything can restart and removed only
        once the bring-up (and any reattach) finishes, so one that failed or
        was interrupted anywhere is retried by the next bring-up even when it
        adopts the pair. The whole sequence holds this backend's bring-up lock.
        Under the default STRICT policy a failure ends the bring-up naming each
        bottle."""
        with self.bring_up_lock():
            marker = self._reattach_pending_marker()
            pending = marker.exists()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            url, restarted = self._bring_up(startup_timeout=startup_timeout)
            if restarted or pending:
                try:
                    reattach_running_bottles(
                        self.gateway_attacher(), orchestrator_url=url,
                        gateway=self.gateway(), policy=attach_policy,
                    )
                except GatewayAttachError as e:
                    die(str(e))
            marker.unlink(missing_ok=True)
            return url

    @contextmanager
    def bring_up_lock(self) -> Generator[None]:
        """Exclusive, host-wide, per-backend lock over infra bring-up and
        anything that must not race it (`rotate-ca`). Re-entrant within a
        process; released if the process dies."""
        with _reentrant_flock(bot_bottle_root() / "infra" / f"{self.name}.lock"):
            yield

    @abc.abstractmethod
    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Bring the pair up; return the host control-plane URL and whether the
        orchestrator or the gateway was (re)started (and so lost its egress
        tokens or per-bottle state) rather than adopted."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove both the orchestrator and the gateway. Idempotent."""

    @abc.abstractmethod
    def gateway_attacher(self) -> GatewayAttacher:
        """How this backend lists its running bottles and reattaches one to
        the gateway (PRD 0081)."""

    def _reattach_pending_marker(self) -> Path:
        return bot_bottle_root() / "infra" / f"{self.name}.reattach-pending"

    def url(self) -> str:
        """The host-facing control-plane URL the CLI reaches (the orchestrator's)."""
        return self.orchestrator().url()

    def is_healthy(self) -> bool:
        """True iff the control plane answers `/health`."""
        return self.orchestrator().is_healthy()


__all__ = ["InfraService"]
