"""`InfraService`: brings up the per-host orchestrator + gateway pair. One impl
per backend (`backend/*/infra.py`).
"""

from __future__ import annotations

import abc
import dataclasses
import fcntl
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ..contract.hostctl import InfraSettings, Settings
from ..control.gateway import Gateway
from ..infra_image import (
    BUILD_NEEDS_DOCKER, published_infra_matches, unpublished_message,
)
from ..lib import log
from ..lib.log import die, info
from ..control.lifecycle import DEFAULT_STARTUP_TIMEOUT_SECONDS, Orchestrator
from ..paths import bot_bottle_root
from ..published_artifacts import FORMAT_OCI
from .gateway_attach import (
    GatewayAttachError,
    GatewayAttacher,
    reattach_running_bottles,
)

# How many times this process holds each lock path.
_HELD: dict[Path, int] = {}


@contextmanager
def _reentrant_flock(path: Path) -> Generator[None]:
    """Exclusive flock on `path`, re-entrant within this process."""
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


def infra_settings_record(backend: str) -> Path:
    """Where `backend`'s last bring-up recorded its `InfraSettings`."""
    return bot_bottle_root() / "infra" / f"{backend}.settings.json"


def _read_record(backend: str) -> dict[str, object] | None:
    try:
        record = json.loads(infra_settings_record(backend).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def recorded_infra_settings(backend: str) -> InfraSettings | None:
    """The `InfraSettings` `backend`'s infra was last (re)started with, or None
    when none is recorded or the record no longer parses."""
    record = _read_record(backend)
    if record is None:
        return None
    try:
        return Settings.from_json({"infra": record.get("infra", {})}).infra
    except ValueError:
        return None


class InfraService(abc.ABC):
    """Compose and bring up the per-host orchestrator + gateway pair.

    `ensure_running` is shared: a backend's `_bring_up` starts or adopts the
    pair and reports whether either plane restarted; only then are running
    bottles reattached."""

    # Names this backend's "reattach pending" marker.
    name: str = "infra"
    settings: Settings
    # The published artifact format the pair runs from, and how a local build
    # of it is described in the stop message.
    infra_format: str = FORMAT_OCI
    infra_build_note: str = BUILD_NEEDS_DOCKER

    @abc.abstractmethod
    def orchestrator(self) -> Orchestrator:
        ...

    @abc.abstractmethod
    def gateway(self) -> Gateway:
        ...

    def ensure_running(
        self,
        *,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        launch_stages: bool = False,
        inherit_build: bool = True,
    ) -> str:
        """Bring the pair up (adopting a healthy one) and reattach running
        bottles if either plane restarted. Returns the host control-plane URL;
        raises `OrchestratorStartError` on timeout.

        Stops before touching anything when nothing published matches this
        install and building locally wasn't asked for (`require_infra_images`).
        `inherit_build` lets an implicit bring-up (a launch) keep building
        locally when the last bring-up was asked to; `bot-bottle up` passes
        False, so it does exactly what its flags say.

        Holds the bring-up lock. A "reattach pending" marker is written first
        and removed only on success, so an interrupted bring-up is retried by
        the next one."""
        if launch_stages:
            log.stage("services", "Starting bot-bottle services")
        try:
            return self._ensure_running(startup_timeout=startup_timeout,
                                        inherit_build=inherit_build)
        finally:
            if launch_stages:
                log.stage("start", "Starting the bottle")

    def require_infra_images(self, *, inherit_build: bool = True) -> None:
        """Die unless the infra images can be pulled (a published release
        matches this install's code) or building them locally was asked for.
        With `inherit_build`, a request that didn't ask takes the opt-in from
        the last recorded bring-up, so `bot-bottle up --build` then
        `bot-bottle <agent>` keeps working (and keeps its fingerprint)."""
        infra = self.settings.infra
        if infra.infra_build_local or published_infra_matches(self.infra_format):
            return
        if inherit_build:
            recorded = recorded_infra_settings(self.name)
            if recorded is not None and recorded.infra_build_local:
                info("no published infra images match this install; building them "
                     "locally, as the last `bot-bottle up --build` asked")
                self.settings = dataclasses.replace(
                    self.settings, infra=dataclasses.replace(infra, infra_build_local=True))
                return
        die(unpublished_message(self.infra_build_note))

    def _ensure_running(self, *, startup_timeout: float, inherit_build: bool) -> str:
        with self.bring_up_lock():
            self.require_infra_images(inherit_build=inherit_build)
            marker = self._reattach_pending_marker()
            pending = marker.exists()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            recorded = self.recorded_infra_fingerprint()
            if (recorded is not None and recorded != self.settings.infra.fingerprint()
                    and self.is_healthy()):
                info(f"{self.name} infra settings changed; bringing it up again with them")
                self.stop()
            url, restarted = self._bring_up(startup_timeout=startup_timeout)
            if restarted:
                self._record_infra_settings()
            if restarted or pending:
                try:
                    reattach_running_bottles(
                        self.gateway_attacher(), orchestrator_url=url,
                        gateway=self.gateway(),
                    )
                except GatewayAttachError as e:
                    die(str(e))
            marker.unlink(missing_ok=True)
            return url

    @contextmanager
    def bring_up_lock(self) -> Generator[None]:
        """Host-wide, per-backend lock over infra bring-up and anything that
        must not race it. Re-entrant within a process."""
        with _reentrant_flock(bot_bottle_root() / "infra" / f"{self.name}.lock"):
            yield

    @abc.abstractmethod
    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Bring the pair up; return the control-plane URL and whether either
        plane was (re)started rather than adopted."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove both the orchestrator and the gateway. Idempotent."""

    @abc.abstractmethod
    def gateway_attacher(self) -> GatewayAttacher:
        """Lists this backend's running bottles and reattaches one to the
        gateway."""

    def _reattach_pending_marker(self) -> Path:
        return bot_bottle_root() / "infra" / f"{self.name}.reattach-pending"

    def _record_infra_settings(self) -> None:
        """Record the `InfraSettings` this bring-up (re)started the pair with."""
        infra = self.settings.infra
        record = {"fingerprint": infra.fingerprint(), "infra": self.settings.to_json()["infra"]}
        path = infra_settings_record(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)

    def recorded_infra_fingerprint(self) -> str | None:
        """Fingerprint of the `InfraSettings` the running pair was started
        with, or None."""
        record = _read_record(self.name)
        fingerprint = record.get("fingerprint") if record is not None else None
        return fingerprint if isinstance(fingerprint, str) else None

    def url(self) -> str:
        """Host-facing control-plane URL."""
        return self.orchestrator().url()

    def is_healthy(self) -> bool:
        return self.orchestrator().is_healthy()


__all__ = ["InfraService", "infra_settings_record", "recorded_infra_settings"]
