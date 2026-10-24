"""What the host controller records: each agent's start and exit
(`lifecycle.*`) and each launch, teardown and refused call it brokers
(`hostctl.*`). A journal that can't be written is logged and skipped, never
blocking a launch or teardown.
"""

from __future__ import annotations

import hashlib
import socket
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ...contract.audit import AuditEvent, canonical, utc_iso
from ...lib.log import warn
from .audit_journal import AuditJournal, AuditJournalBusy

if TYPE_CHECKING:
    from ...manifest import Manifest

PRODUCER = "hostctl"
ACTOR = "hostctl"


def digest_manifest(manifest: "Manifest") -> str:
    """The sealed manifest a bottle runs under (its policy); "" (logged) if it
    can't be rendered."""
    from ...manifest.preview import manifest_to_yaml  # pylint: disable=import-outside-toplevel
    try:
        rendered = manifest_to_yaml(manifest)
    except Exception as e:  # pylint: disable=broad-exception-caught
        warn(f"audit: couldn't digest the manifest: {e}")
        return ""
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def digest_request(body: Mapping[str, Any]) -> str:
    """A request's fingerprint: secret names kept, values left out."""
    scrubbed = dict(body)
    if isinstance(scrubbed.get("secrets"), Mapping):
        scrubbed["secrets"] = sorted(scrubbed["secrets"])
    return "sha256:" + hashlib.sha256(canonical(scrubbed).encode("utf-8")).hexdigest()


class AuditUnavailable(Exception):
    """This host controller has no journal open."""


class HostAudit:
    def __init__(self, journal: AuditJournal | None = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._journal = journal
        self._clock = clock
        self._listeners: list[Callable[[AuditEvent, str], None]] = []

    def listen(self, listener: Callable[[AuditEvent, str], None]) -> None:
        """Call `listener(event, producer)` for each event another component
        sends, once, whether or not the journal could write it."""
        self._listeners.append(listener)

    def _notify(self, event: AuditEvent, producer: str) -> None:
        for listener in self._listeners:
            try:
                listener(event, producer)
            except Exception as e:  # pylint: disable=broad-exception-caught
                warn(f"audit: a listener failed on {event.type}: {e}")

    @classmethod
    def open(cls) -> "HostAudit":
        """The host's journal, or a no-op recorder when it can't be opened."""
        # pylint: disable=import-outside-toplevel
        from ...engine import engine_id
        from ...paths import host_audit_dir
        try:
            return cls(AuditJournal(host_audit_dir(), host=socket.gethostname(),
                                    engine=engine_id()))
        except (AuditJournalBusy, OSError, ValueError) as e:
            warn(f"audit journal unavailable, not recording: {e}")
            return cls(None)

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    def record(self, event: AuditEvent) -> None:
        if self._journal is None:
            return
        try:
            self._journal.append(event, producer=PRODUCER)
        except Exception as e:  # pylint: disable=broad-exception-caught
            warn(f"audit: couldn't record {event.type}: {e}")

    def append(self, event: AuditEvent, *, producer: str) -> bool:
        """Write an event another component sent. False for an already-written
        retry; AuditRedactionError for a credential-shaped key."""
        if self._journal is None:
            self._notify(event, producer)
            raise AuditUnavailable("audit journal unavailable")
        written = self._journal.append(event, producer=producer) is not None
        if written:  # a retry was already heard
            self._notify(event, producer)
        return written

    def _now(self) -> str:
        return utc_iso(self._clock())

    # --- lifecycle.* ---

    def bottled_agent_start(self, *, bottle: str, activation: str, manifest_digest: str,
                            backend: str, agent: str) -> None:
        self.record(AuditEvent(
            type="lifecycle.bottled_agent_start", actor=ACTOR,
            action="lifecycle.bottled_agent_start", outcome="success", sensitivity="normal",
            ts_event=self._now(), resource=bottle, bottle=bottle, bottled_agent=bottle,
            activation=activation, manifest_digest=manifest_digest, correlation_id=activation,
            payload={"manifest_digest": manifest_digest, "backend": backend, "agent": agent},
        ))

    def bottled_agent_exit(self, *, bottle: str, activation: str, manifest_digest: str,
                           exit_code: int, stop_requested: bool) -> None:
        """`_stop` for a clean or requested exit, `_crash` for anything else."""
        crashed = exit_code != 0 and not stop_requested
        kind = "lifecycle.bottled_agent_crash" if crashed else "lifecycle.bottled_agent_stop"
        self.record(AuditEvent(
            type=kind, actor=ACTOR, action=kind,
            outcome="failure" if crashed else "success", sensitivity="normal",
            ts_event=self._now(), resource=bottle, bottle=bottle, bottled_agent=bottle,
            activation=activation, manifest_digest=manifest_digest, correlation_id=activation,
            payload={"manifest_digest": manifest_digest, "exit": exit_code,
                     "stop_requested": stop_requested},
        ))

    # --- hostctl.* ---

    def brokered(self, kind: str, op: str, *, role: str, success: bool,
                 request_digest: str, bottle: str | None = None,
                 detail: str | None = None) -> None:
        """`hostctl.broker_launch` / `broker_teardown`: a job run for `role`."""
        payload: dict[str, Any] = {"op": op, "request_digest": request_digest}
        if detail:
            payload["detail"] = detail
        self.record(AuditEvent(
            type=f"hostctl.{kind}", actor=f"role:{role}", action=op,
            outcome="success" if success else "failure", sensitivity="security",
            ts_event=self._now(), resource=bottle, bottle=bottle, bottled_agent=bottle,
            payload=payload,
        ))

    def rejected(self, op: str, *, role: str | None, request_digest: str) -> None:
        """`hostctl.broker_reject`: a call refused for its token or role."""
        self.record(AuditEvent(
            type="hostctl.broker_reject", actor=f"role:{role}" if role else "unauthenticated",
            action=op, outcome="blocked", sensitivity="security", ts_event=self._now(),
            payload={"op": op, "request_digest": request_digest},
        ))


__all__ = ["AuditUnavailable", "HostAudit", "PRODUCER", "digest_manifest", "digest_request"]
