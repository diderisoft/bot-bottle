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
                            backend: str, agent: str,
                            bottle_versions: Mapping[str, object] | None = None) -> None:
        """`bottle_versions`: the bottle files' versions it launched from
        (`manifest.versions.launch_versions`)."""
        self.record(AuditEvent(
            type="lifecycle.bottled_agent_start", actor=ACTOR,
            action="lifecycle.bottled_agent_start", outcome="success", sensitivity="normal",
            ts_event=self._now(), resource=bottle, bottle=bottle, bottled_agent=bottle,
            activation=activation, manifest_digest=manifest_digest, correlation_id=activation,
            payload={"manifest_digest": manifest_digest, "backend": backend, "agent": agent,
                     **({"bottle_versions": dict(bottle_versions)} if bottle_versions else {})},
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

    # --- transcript.* ---

    def transcript_captured(self, *, bottle: str, activation: str, copy: str, guest_path: str,
                            size: int, sha256: str, final: bool) -> None:
        """A checkpoint of a session's host transcript copy: its size and
        running SHA-256. The content is the agent's account (agent-claimed);
        the guest path is the bottle's to choose, so it's untrusted."""
        self.record(AuditEvent(
            type="transcript.captured", actor=ACTOR, action="transcript.captured",
            outcome="success", sensitivity="normal", ts_event=self._now(), resource=copy,
            bottle=bottle, bottled_agent=bottle, activation=activation,
            correlation_id=activation,
            payload={"copy": copy, "bytes": size, "sha256": sha256, "final": final,
                     "provenance": "agent-claimed"},
            untrusted={"guest_path": guest_path},
        ))

    def transcript_rewritten(self, *, bottle: str, activation: str, copy: str, new_copy: str,
                             guest_path: str, offset: int, size: int) -> None:
        """A transcript file changed before the bytes already copied: `copy`
        is kept as it was and `new_copy` holds the file as it is now."""
        self.record(AuditEvent(
            type="transcript.rewritten", actor=ACTOR, action="transcript.rewritten",
            outcome="success", sensitivity="security", ts_event=self._now(), resource=copy,
            bottle=bottle, bottled_agent=bottle, activation=activation,
            correlation_id=activation,
            payload={"copy": copy, "new_copy": new_copy, "copied_bytes": offset,
                     "guest_bytes": size, "provenance": "agent-claimed"},
            untrusted={"guest_path": guest_path},
        ))

    def transcript_deleted(self, *, bottle: str, activation: str, copies: list[dict[str, Any]],
                           retention_days: int) -> None:
        """A run's transcript copies, deleted after `retention_days`: what
        each held, by size and digest."""
        self.record(AuditEvent(
            type="transcript.deleted", actor=ACTOR, action="transcript.deleted",
            outcome="success", sensitivity="normal", ts_event=self._now(),
            resource=f"{bottle}/{activation}", bottle=bottle, bottled_agent=bottle,
            activation=activation, correlation_id=activation,
            payload={"copies": copies, "retention_days": retention_days},
        ))

    def snapshot_taken(self, *, bottle: str, activation: str, snapshot: dict[str, Any]) -> None:
        """A snapshot of a bottle's workspace, taken from the console: the
        patch's digest and size, and the conversation position it pins."""
        self.record(AuditEvent(
            type="snapshot.taken", actor=ACTOR, action="snapshot.taken", outcome="success",
            sensitivity="normal", ts_event=self._now(),
            resource=f"{bottle}/{activation}/{snapshot.get('id', '')}", bottle=bottle,
            bottled_agent=bottle, activation=activation, correlation_id=activation,
            payload={k: snapshot.get(k) for k in ("id", "seq", "files", "added", "removed",
                                                   "bytes", "sha256")},
        ))

    def credential_event(self, kind: str, *, bottle: str, activation: str, harness: str,
                         **payload: object) -> None:
        """`credential.renewed` / `credential.renew_failed`: a running bottle's
        forwarded host login was refreshed, or couldn't be. Never the value."""
        self.record(AuditEvent(
            type=kind, actor=ACTOR, action=kind,
            outcome="failure" if kind.endswith("_failed") else "success",
            sensitivity="normal", ts_event=self._now(), resource=f"{bottle}/{harness}",
            bottle=bottle, bottled_agent=bottle, activation=activation,
            correlation_id=activation, payload={"harness": harness, **payload},
        ))

    def audit_records_read(self, *, run_id: str, count: int, credential_id: str) -> None:
        """`audit.records_read`: audit records decrypted for the console, with
        the passkey that unlocked it (ADR 0013)."""
        self.record(AuditEvent(
            type="audit.records_read", actor=f"passkey:{credential_id}",
            action="audit.records_read", outcome="success", sensitivity="security",
            ts_event=self._now(), resource=run_id, activation=run_id, correlation_id=run_id,
            payload={"run_id": run_id, "records": count},
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
