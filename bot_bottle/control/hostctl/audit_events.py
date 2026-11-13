"""What the host controller records: each agent's start and exit
(`lifecycle.*`) and each launch, teardown and refused call it brokers
(`hostctl.*`). A journal that can't be written is logged and skipped, never
blocking a launch or teardown.
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ...contract.audit import AuditEvent, canonical, utc_iso
from ...lib.log import warn
from .audit_journal import AuditJournal, AuditJournalBusy, credential_shaped

if TYPE_CHECKING:
    from ...manifest import Manifest

PRODUCER = "hostctl"
ACTOR = "hostctl"


def manifest_content(manifest: "Manifest") -> dict[str, Any]:
    """What a bottle runs under, kept with its start so the trail says what
    ran even after the files change: the resolved bottle as one document
    (`manifest.document`, its whole `extends` chain, secrets by name as the
    files have them) and the resolved agent. Kept out of it:

    - a value from the host secrets file, wherever it appears, as
      `[secret:NAME]`;
    - a token-shaped string, as the journal scrubs one, so the record holds
      exactly what was hashed;
    - under a key the journal reads as a credential's: a reference
      (`secret:NAME`, `${VAR}`) is kept, as `<key>_ref`; anything else is a
      value written in (pi's `settings.api_key`, a `DB_PASSWORD` env), as
      `<key>_redacted: [redacted]`.

    Empty (logged) if it can't be rendered."""
    # pylint: disable=import-outside-toplevel
    from dataclasses import asdict
    from ...manifest.document import bottle_document
    from ...manifest.versions import secret_values
    try:
        agent = asdict(manifest.agent)
        provider = agent.get("agent_provider")
        if isinstance(provider, dict) and provider.get("auth_token"):
            # A secret's bare name: as the agent file writes it.
            provider["auth_token"] = f"secret:{provider['auth_token']}"
        content = json.loads(json.dumps(
            {"bottle": bottle_document(manifest.bottle), "agent": agent}, default=str))
        return _scrubbed(_redacted(_refs(content), secret_values()))
    except Exception as e:  # pylint: disable=broad-exception-caught
        warn(f"audit: couldn't record the manifest: {e}")
        return {}


def _is_ref(value: Any) -> bool:
    """A reference to a secret (`secret:NAME`, `${VAR}`, `secret:${VAR}`, or
    `?message`, asked at launch), or nothing: what a credential-shaped key may
    hold in what's recorded."""
    # pylint: disable-next=import-outside-toplevel
    from ... import secret_ref
    if value in ("", None):
        return True
    return isinstance(value, str) and secret_ref.kind(value) in (
        secret_ref.STORED, secret_ref.HOST_ENV, secret_ref.PROMPT)


def _refs(value: Any) -> Any:
    """`value` with each credential-shaped key, which the journal refuses,
    renamed: `<key>_ref` when it holds a reference, else `<key>_redacted`
    with its value left out."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not credential_shaped(k):
                out[k] = _refs(v)
            elif _is_ref(v):
                out[f"{k}_ref"] = v
            else:
                out[f"{k}_redacted"] = "[redacted]"
        return out
    if isinstance(value, list):
        return [_refs(v) for v in value]
    return value


def _scrubbed(value: Any) -> Any:
    """`value` with token-shaped strings scrubbed, as the journal does to a
    payload: the digest is of what's stored."""
    # pylint: disable-next=import-outside-toplevel
    from ...gateway.egress.dlp_detectors import redact_tokens
    if isinstance(value, dict):
        return {k: _scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    return redact_tokens(value) if isinstance(value, str) else value


def _redacted(value: Any, secrets: Mapping[str, str]) -> Any:
    """`value` with each secret's value, wherever it appears in a string,
    replaced by `[secret:NAME]`."""
    if isinstance(value, dict):
        return {k: _redacted(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, secrets) for v in value]
    if isinstance(value, str):
        # The longest first, so one secret holding another is kept out whole.
        for name, secret in sorted(secrets.items(), key=lambda s: -len(s[1])):
            value = value.replace(secret, f"[secret:{name}]")
    return value


def digest_content(content: Mapping[str, Any]) -> str:
    """`manifest_content`'s fingerprint (the policy a bottle runs under):
    the SHA-256 of its canonical form, so it changes with any change to what
    runs and anyone holding the content can check it. "" for none."""
    if not content:
        return ""
    return "sha256:" + hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()


def digest_manifest(manifest: "Manifest") -> str:
    """The digest of what `manifest` runs under; "" (logged) if it can't be
    rendered."""
    return digest_content(manifest_content(manifest))


def digest_request(body: Mapping[str, Any]) -> str:
    """A request's fingerprint: secret names kept, values left out."""
    scrubbed = dict(body)
    for key in ("secrets", "answers"):
        if isinstance(scrubbed.get(key), Mapping):
            scrubbed[key] = sorted(scrubbed[key])
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

    def prune(self, *, max_age_days: int, max_bytes: int,
              keep_from: str) -> dict[str, Any] | None:
        """Prune the journal's old segments (`AuditJournal.prune`); None when
        nothing went or there's no journal."""
        if self._journal is None:
            return None
        return self._journal.prune(max_age_days=max_age_days, max_bytes=max_bytes,
                                   keep_from=keep_from)

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
                            bottle_versions: Mapping[str, object] | None = None,
                            manifest: Mapping[str, Any] | None = None) -> None:
        """`bottle_versions`: the bottle files' versions it launched from
        (`manifest.versions.launch_versions`). `manifest`: what it runs
        under (`manifest_content`), whose digest is `manifest_digest`."""
        self.record(AuditEvent(
            type="lifecycle.bottled_agent_start", actor=ACTOR,
            action="lifecycle.bottled_agent_start", outcome="success", sensitivity="normal",
            ts_event=self._now(), resource=bottle, bottle=bottle, bottled_agent=bottle,
            activation=activation, manifest_digest=manifest_digest, correlation_id=activation,
            payload={"manifest_digest": manifest_digest, "backend": backend, "agent": agent,
                     **({"bottle_versions": dict(bottle_versions)} if bottle_versions else {}),
                     **({"manifest": dict(manifest)} if manifest else {})},
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


__all__ = ["AuditUnavailable", "HostAudit", "PRODUCER", "digest_content", "digest_manifest",
           "digest_request", "manifest_content"]
