"""`InfraService`: brings up the per-host orchestrator + gateway pair. One impl
per backend (`backend/*/infra.py`).
"""

from __future__ import annotations

import abc
import dataclasses
import fcntl
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from .. import infra_state
from ..contract.hostctl import InfraSettings, Settings
from ..control.client import OrchestratorClient, record_orchestrator, recorded_orchestrator
from ..control.gateway import Gateway
from ..environment import active_name
from ..infra_image import (
    BUILD_NEEDS_DOCKER, published_infra_matches, unpublished_message,
)
from ..lib import log
from ..lib.log import die, info
from ..control.lifecycle import DEFAULT_STARTUP_TIMEOUT_SECONDS, Orchestrator
from ..paths import bot_bottle_root, private_dir
from ..published_artifacts import FORMAT_OCI
from .gateway_attach import (
    AttachPolicy,
    GatewayAttachError,
    GatewayAttacher,
    reattach_running_bottles,
)

def _infra_state_dir() -> Path:
    """`<root>/infra/`, 0700 (`paths.infra_state_dir`)."""
    return private_dir(bot_bottle_root() / "infra")


# How many times each thread holds each lock path. Per thread: the host
# controller brings infra up from more than one (the architect's bottle and a
# launch), and a process-wide count let a second thread in without the lock.
_HELD: dict[tuple[Path, int], int] = {}


@contextmanager
def _reentrant_flock(path: Path) -> Generator[None]:
    """Exclusive flock on `path`, re-entrant within a thread. Another thread
    of this process waits like another process: its own open of the file
    can't take the lock while this one holds it."""
    key = (path, threading.get_ident())
    if _HELD.get(key):
        _HELD[key] += 1
        try:
            yield
        finally:
            _HELD[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _HELD[key] = 1
        try:
            yield
        finally:
            del _HELD[key]
            fcntl.flock(handle, fcntl.LOCK_UN)


def recorded_infra_settings(backend: str) -> InfraSettings | None:
    """The `InfraSettings` `backend`'s infra was last (re)started with
    (`infra_state`), or None when none is recorded or it no longer parses."""
    raw = infra_state.get(f"{backend}.settings")
    if not raw:
        return None
    try:
        return Settings.from_json({"infra": json.loads(raw)}).infra
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
        attach_policy: AttachPolicy = AttachPolicy.STRICT,
    ) -> str:
        """Bring the pair up (adopting a healthy one) and reattach running
        bottles if either plane restarted. Returns the host control-plane URL;
        raises `OrchestratorStartError` on timeout.

        Stops before touching anything when nothing published matches this
        install and building locally wasn't asked for (`require_infra_images`).
        `inherit_build` lets an implicit bring-up (a launch) keep building
        locally when the last bring-up was asked to; `bot-bottle up` passes
        False, so it does exactly what its flags say.

        Records this backend's orchestrator as the environment's (what every
        later call to "the orchestrator" uses), and dies instead when another
        backend's orchestrator is recorded and still answers: an environment
        runs exactly one.

        Holds the bring-up lock. A "reattach pending" marker is written first
        and removed only on success, so an interrupted or failed reattach is
        retried by the next bring-up (a bottle stopped meanwhile drops out of
        it). With `attach_policy=AttachPolicy.REPORT` (`bot-bottle up
        --allow-reattach-failures`), bottles that won't reattach are named and
        the bring-up succeeds, clearing the marker."""
        if launch_stages:
            log.stage("services", "Starting bot-bottle services")
        try:
            return self._ensure_running(startup_timeout=startup_timeout,
                                        inherit_build=inherit_build,
                                        attach_policy=attach_policy)
        finally:
            if launch_stages:
                log.stage("start", "Starting the bottle")

    def require_infra_images(self, *, inherit_build: bool = True,
                             adopting: bool = True) -> None:
        """Die unless the infra images can be pulled (a published release
        matches this install's code) or building them locally was asked for.
        With `inherit_build`, a request that didn't ask takes the opt-in from
        the last recorded bring-up, so `bot-bottle up --build` then
        `bot-bottle <agent>` keeps working (and keeps its fingerprint).

        With `adopting` (any bring-up that doesn't stop the pair first), a
        running, healthy pair that differs from this request only in that
        opt-in is kept as it is, opt-in included: nothing needs building or
        booting, and dropping the opt-in would make the next bring-up need
        images there are none of. A cold start, or a forced restart
        (`adopting=False`), still needs the images or `--build`."""
        infra = self.settings.infra
        if infra.infra_build_local or published_infra_matches(self.infra_format):
            return
        if adopting and self._current_local_build_running():
            info(f"the {self.name} infra pair is running and current: keeping it, and the "
                 "last `bot-bottle up --build` with it (no published infra images match this "
                 "install, so it stays built locally; `bot-bottle up --restart-infra --build` "
                 "rebuilds it)")
            self.settings = dataclasses.replace(
                self.settings, infra=dataclasses.replace(infra, infra_build_local=True))
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

    def _current_local_build_running(self) -> bool:
        """Whether the running pair was last brought up with `--build`, is
        healthy, and its recorded settings are this request's but for that
        opt-in."""
        recorded = recorded_infra_settings(self.name)
        if recorded is None or not recorded.infra_build_local:
            return False
        local = dataclasses.replace(self.settings.infra, infra_build_local=True)
        if self.recorded_infra_fingerprint() != local.fingerprint():
            return False
        return self.is_healthy()

    def _ensure_running(self, *, startup_timeout: float, inherit_build: bool,
                        attach_policy: AttachPolicy) -> str:
        env_lock = _infra_state_dir() / "orchestrator.lock"
        with self.bring_up_lock(), _reentrant_flock(env_lock):
            self._refuse_second_orchestrator()
            self.require_infra_images(inherit_build=inherit_build)
            marker = self._reattach_pending_marker()
            pending = marker.exists()
            marker.touch()
            recorded = self.recorded_infra_fingerprint()
            if (recorded is not None and recorded != self.settings.infra.fingerprint()
                    and self.is_healthy()):
                info(f"{self.name} infra settings changed; bringing it up again with them")
                self.stop()
            url, restarted = self._bring_up(startup_timeout=startup_timeout)
            # The environment's one orchestrator, for everything that talks to it.
            record_orchestrator(self.name, url)
            if restarted:
                self._record_infra_settings()
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

    def _refuse_second_orchestrator(self) -> None:
        """Die if the environment's recorded orchestrator is another backend's
        and still answers: an environment runs exactly one orchestrator. One
        that's gone is replaced by this bring-up."""
        current = recorded_orchestrator()
        if current is None or current.backend == self.name:
            return
        if not OrchestratorClient(current.url).health():
            return
        die(f"environment {active_name()} already runs the {current.backend} orchestrator "
            f"(at {current.url}), and an environment runs one; use the {current.backend} "
            f"backend (`--backend {current.backend}`), stop it first "
            f"(`bot-bottle down --backend {current.backend}`), or use another "
            "environment (`BOT_BOTTLE_ENV=<name>`)")

    @contextmanager
    def bring_up_lock(self) -> Generator[None]:
        """Host-wide, per-backend lock over infra bring-up and anything that
        must not race it. Re-entrant within a process."""
        with _reentrant_flock(_infra_state_dir() / f"{self.name}.lock"):
            yield

    @abc.abstractmethod
    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Bring the pair up; return the control-plane URL and whether either
        plane was (re)started rather than adopted."""

    def prepare_images(self) -> None:
        """Make this bring-up's images ready without touching the running
        pair, so a restart that has to build doesn't do it while the gateway
        is down. Backends that build inside `_bring_up` before replacing
        anything need nothing here."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove both the orchestrator and the gateway. Idempotent."""

    @abc.abstractmethod
    def gateway_attacher(self) -> GatewayAttacher:
        """Lists this backend's running bottles and reattaches one to the
        gateway."""

    def _reattach_pending_marker(self) -> Path:
        return _infra_state_dir() / f"{self.name}.reattach-pending"

    def _record_infra_settings(self) -> None:
        """Record the `InfraSettings` this bring-up (re)started the pair with."""
        infra = self.settings.infra
        infra_state.record({
            f"{self.name}.settings": json.dumps(self.settings.to_json()["infra"],
                                                sort_keys=True),
            f"{self.name}.fingerprint": infra.fingerprint()})

    def recorded_infra_fingerprint(self) -> str | None:
        """Fingerprint of the `InfraSettings` the running pair was started
        with, or None."""
        return infra_state.get(f"{self.name}.fingerprint") or None

    def url(self) -> str:
        """Host-facing control-plane URL."""
        return self.orchestrator().url()

    def is_healthy(self) -> bool:
        return self.orchestrator().is_healthy()


__all__ = ["InfraService", "recorded_infra_settings"]
