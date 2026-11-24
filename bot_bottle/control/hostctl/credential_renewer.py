"""Scheduled renewal for expiring credentials declared in `credentials:`.

A bottle's `credentials:` block may declare credentials with sources that
mint short-lived values (`host_login`, `assume_role`, …). The `CredentialRenewer`
runs a background thread that checks every `RENEW_CHECK_SECONDS` and calls
`CredentialProvider.renew()` on any credential whose expiry is within its
renew-before window.

On teardown, `retire()` is called once per bottle to run `retire()` on each
of its credentials (revoke deploy keys, etc.).

Design:
  - Each bottle registers a `_BottleEntry` at launch.
  - The timer thread calls `_renew_bottle()` for every registered bottle.
  - `retire_bottle()` removes the bottle and retires each credential.
  - Push: same `update_agent_secret` path as `CredentialRefresher`, via the
    `Push` callback.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ...credentials import (
    CredentialProvider, MintedCredential,
)
from ...lib import log

if TYPE_CHECKING:
    from ...secret_source import SecretSource

# How often the renewer polls for near-expiry credentials.
RENEW_CHECK_SECONDS = 60.0
# Renew at least this long before expiry.
MIN_RENEW_BEFORE = timedelta(minutes=10)
# Also renew if less than this fraction of original lifetime remains.
RENEW_BEFORE_FRACTION = 0.20

# (registry_id, slot, value, env_var_secret) → pushed, or raises.
Push = Callable[[str, str, str, str], None]
# (bottle_id, label, values) → pushed for agent_can_read credentials.
ReadablePush = Callable[[str, str, "dict[str, str]"], None]


def _renew_before(issued_at: datetime, expires_at: datetime) -> timedelta:
    """How long before `expires_at` to trigger renewal."""
    lifetime = expires_at - issued_at
    return max(MIN_RENEW_BEFORE, lifetime * RENEW_BEFORE_FRACTION)


@dataclass
class _CredEntry:
    """One credential tracked for a bottle."""
    label: str
    cred: MintedCredential
    issued_at: datetime
    provider: CredentialProvider
    settings: Mapping[str, object]
    secrets: "SecretSource"
    # Egress slot names for this credential's value. Maps each field name
    # (usually "token") to the EGRESS_TOKEN_N slots that carry it.
    slots: dict[str, tuple[str, ...]]

    def should_renew(self, now: datetime) -> bool:
        if self.cred.expires_at is None:
            return False
        before = _renew_before(self.issued_at, self.cred.expires_at)
        return self.cred.expires_at - now <= before


@dataclass
class _BottleEntry:
    bottle_id: str
    registry_id: str
    env_var_secret: str
    creds: dict[str, _CredEntry] = field(default_factory=dict)
    warned: dict[str, str] = field(default_factory=dict)
    readable_labels: frozenset[str] = frozenset()


class CredentialRenewer:
    """Keeps `credentials:` block credentials current for running bottles."""

    def __init__(
        self,
        push: Push,
        *,
        audit: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        readable_push: "ReadablePush | None" = None,
    ) -> None:
        self._push = push
        self._audit = audit
        self._clock = clock
        self._readable_push = readable_push
        self._bottles: dict[str, _BottleEntry] = {}
        self._lock = threading.Lock()
        self._renewing = threading.Lock()
        self._stop = threading.Event()

    def add(
        self,
        bottle_id: str,
        registry_id: str,
        env_var_secret: str,
        credentials: list[tuple[
            str,                   # label
            MintedCredential,
            CredentialProvider,
            Mapping[str, object],  # settings
            "SecretSource",
            dict[str, tuple[str, ...]],  # slots: field → EGRESS_TOKEN_N names
        ]],
        *,
        readable_labels: frozenset[str] = frozenset(),
    ) -> None:
        """Register a bottle's credentials for renewal. `credentials` is a list
        of (label, minted, provider, settings, secrets, slots) tuples, one per
        credential entry in the bottle's `credentials:` block.
        `readable_labels` names any `agent_can_read: true` credentials so the
        renewer can push fresh values via `readable_push` on renewal."""
        now = self._clock()
        entry = _BottleEntry(bottle_id=bottle_id, registry_id=registry_id,
                             env_var_secret=env_var_secret,
                             readable_labels=readable_labels)
        for label, minted, provider, settings, secrets, slots in credentials:
            entry.creds[label] = _CredEntry(
                label=label, cred=minted, issued_at=now,
                provider=provider, settings=settings,
                secrets=secrets, slots=slots,
            )
        with self._lock:
            self._bottles[bottle_id] = entry

    def remove(self, bottle_id: str) -> None:
        """Stop tracking a bottle (without retiring its credentials)."""
        with self._lock:
            self._bottles.pop(bottle_id, None)

    def retire(self, bottle_id: str) -> None:
        """Call `retire()` on each credential and remove the bottle."""
        with self._lock:
            entry = self._bottles.pop(bottle_id, None)
        if entry is None:
            return
        for ce in entry.creds.values():
            try:
                ce.provider.retire(ce.label, bottle_id, ce.cred)
                self._record(entry, "credential.retired", label=ce.label,
                             cred_kind=ce.cred.kind, source=ce.provider.source,
                             principal=ce.cred.principal)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warn(f"bottle {bottle_id}: couldn't retire credential {ce.label!r}: {e}")

    def start(self) -> None:
        """Start the background renewal thread."""
        def loop() -> None:
            while not self._stop.wait(RENEW_CHECK_SECONDS):
                self.renew_all()
        threading.Thread(target=loop, daemon=True, name="hostctl-cred-renewer").start()

    def stop(self) -> None:
        self._stop.set()

    def status_for(self, bottle_id: str) -> list[dict[str, object]]:
        """Credential metadata for `bottle_id`'s tracked credentials (no values)."""
        with self._lock:
            entry = self._bottles.get(bottle_id)
        if entry is None:
            return []
        out = []
        for ce in entry.creds.values():
            out.append({
                "label": ce.label,
                "kind": ce.cred.kind,
                "principal": ce.cred.principal,
                "expires_at": ce.cred.expires_at.isoformat() if ce.cred.expires_at else None,
                "agent_can_read": ce.label in entry.readable_labels,
            })
        return out

    def renew_all(self) -> None:
        """Check and renew all near-expiry credentials across all bottles."""
        with self._lock:
            bottles = list(self._bottles.values())
        for entry in bottles:
            self._renew_bottle(entry)

    def _renew_bottle(self, entry: _BottleEntry) -> None:
        now = self._clock()
        for ce in list(entry.creds.values()):
            if not ce.should_renew(now):
                continue
            try:
                self._renew_one(entry, ce)
            except Exception as e:  # pylint: disable=broad-exception-caught  # one credential mustn't stop the rest
                self._warn(entry, ce.label, f"renewal failed: {e}")
                self._record(entry, "credential.renew_failed", label=ce.label,
                             cred_kind=ce.cred.kind, source=ce.provider.source, reason=str(e))

    def _renew_one(self, entry: _BottleEntry, ce: _CredEntry) -> None:
        """Renew one credential and push changed values."""
        with self._renewing:
            fresh = ce.provider.renew(ce.label, entry.bottle_id, ce.cred,
                                      ce.settings, ce.secrets)
        changed = {
            field: fresh.values[field]
            for field in fresh.values
            if ce.cred.values.get(field) != fresh.values.get(field)
        }
        if not changed:
            return
        for field_name, new_value in changed.items():
            for slot in ce.slots.get(field_name, ()):
                self._push(entry.registry_id, slot, new_value, entry.env_var_secret)
        if ce.label in entry.readable_labels and self._readable_push is not None:
            try:
                self._readable_push(entry.bottle_id, ce.label, dict(fresh.values))
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warn(f"bottle {entry.bottle_id}: couldn't push renewed readable "
                         f"credential {ce.label!r}: {e}")
        log.info(f"bottle {entry.bottle_id}: renewed credential {ce.label!r}"
                 + (f" (valid until {fresh.expires_at:%Y-%m-%d %H:%M} UTC)"
                    if fresh.expires_at else ""))
        self._record(entry, "credential.renewed", label=ce.label,
                     cred_kind=fresh.kind, source=ce.provider.source,
                     principal=fresh.principal,
                     expires_at=fresh.expires_at.isoformat() if fresh.expires_at else "")
        # Update the in-memory credential.
        ce.cred = fresh
        ce.issued_at = self._clock()

    def _warn(self, entry: _BottleEntry, label: str, message: str) -> bool:
        key = f"{label}:{message}"
        if entry.warned.get(label) == key:
            return False
        entry.warned[label] = key
        log.warn(f"bottle {entry.bottle_id} credential {label!r}: {message}")
        return True

    def _record(self, entry: _BottleEntry, event_kind: str, **payload: object) -> None:
        record = getattr(self._audit, "credential_event", None)
        if record is not None:
            record(event_kind, bottle=entry.bottle_id, activation=entry.registry_id,
                   harness="credentials", **payload)


__all__ = [
    "CredentialRenewer", "ReadablePush",
    "RENEW_CHECK_SECONDS", "MIN_RENEW_BEFORE", "RENEW_BEFORE_FRACTION",
]
