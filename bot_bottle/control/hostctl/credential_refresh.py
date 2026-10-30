"""Keep running bottles' forwarded host logins current.

A bottle with `forward_host_credentials` has the host's Claude or Codex login
read once at launch; the gateway injects it and the agent holds a placeholder.
That copy expires (Codex's after about ten days, Claude's sooner) while the
host's own CLI keeps refreshing its login. Every REFRESH_SECONDS the host
controller re-reads the host's login through the agent provider
(`refresh_credentials`) and pushes a value that changed to the orchestrator,
which the gateway reads per request, so the bottle keeps working without a
relaunch. A host login that's missing, expired or close to expiring is
reported (log and audit) with what to run on the host; bot-bottle never spends
the host's refresh token itself (#769).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ...errors import HostLoginError
from ...lib import log

if TYPE_CHECKING:
    from ...backend import Bottle
    from ...backend.adoption import AdoptedCredentials

REFRESH_SECONDS = 300.0
READ_TIMEOUT_SECONDS = 60.0  # reading the bottle's secret from inside it
# How long before the host login expires to start warning.
WARN_BEFORE = timedelta(hours=6)
# Where firecracker keeps the bottle's ENV_VAR_SECRET; the container backends
# put it in the agent's environment.
_ENV_VAR_SECRET_FILE = "/run/bot-bottle/env-var-secret"

# (registry id, egress slot, value, env_var_secret) -> pushed, or raises.
Push = Callable[[str, str, str, str], None]


@dataclass
class _Target:
    identity: str
    credentials: "AdoptedCredentials"
    registry_id: str
    bottle: "Bottle"
    env_var_secret: str = ""
    pushed: dict[str, str] = field(default_factory=dict)
    warned: str = ""


def read_env_var_secret(bottle: "Bottle") -> str:
    """The bottle's ENV_VAR_SECRET, read from inside it, for a bottle a
    previous host controller launched."""
    # pylint: disable-next=import-outside-toplevel
    from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
    result = bottle.exec(f"cat {_ENV_VAR_SECRET_FILE} 2>/dev/null || "
                         f"printenv {ENV_VAR_SECRET_NAME}", user="root",
                         timeout=READ_TIMEOUT_SECONDS)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


class CredentialRefresher:
    """The running bottles whose forwarded host login is kept current."""

    def __init__(self, push: Push, *, audit: object | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._push = push
        self._audit = audit
        self._clock = clock
        self._targets: dict[str, _Target] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def add(self, identity: str, credentials: "AdoptedCredentials", *, registry_id: str,
            bottle: "Bottle", env_var_secret: str = "",
            current: Mapping[str, str] | None = None) -> None:
        """Keep `identity`'s login current. `current`: the values it launched
        with, so an unchanged login isn't pushed again."""
        with self._lock:
            self._targets[identity] = _Target(
                identity, credentials, registry_id, bottle, env_var_secret,
                dict(current or {}))

    def remove(self, identity: str) -> None:
        with self._lock:
            self._targets.pop(identity, None)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(REFRESH_SECONDS):
                self.refresh_all()
        threading.Thread(target=loop, daemon=True, name="hostctl-credentials").start()

    def stop(self) -> None:
        self._stop.set()

    def refresh_all(self) -> None:
        with self._lock:
            targets = list(self._targets.values())
        for target in targets:
            try:
                self.refresh(target)
            except Exception as e:  # pylint: disable=broad-exception-caught  # one bottle mustn't stop the rest
                self._warn(target, f"couldn't refresh its {target.credentials.harness} login: {e}")

    def refresh_now(self, identity: str) -> dict[str, object]:
        """Refresh `identity`'s login now (the console's Refresh credentials):
        `refreshed` (whether a new value was pushed) and `expires_at`, or
        `error` saying why not."""
        with self._lock:
            target = self._targets.get(identity)
        if target is None:
            return {"error": "Nothing to refresh: this agent doesn't forward a host "
                             "login, or was started before bot-bottle refreshed them. "
                             "Stop and Resume it to have it refreshed (its conversation "
                             "continues)."}
        try:
            return self.refresh(target)
        except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
            self._warn(target, f"couldn't refresh its {target.credentials.harness} login: {e}")
            return {"error": f"couldn't refresh it: {e}"}

    def refresh(self, target: _Target) -> dict[str, object]:
        """One refresh of `target`: what `refresh_now` reports."""
        # pylint: disable-next=import-outside-toplevel
        from ...agent_provider import CredentialRefreshSpec, get_provider
        creds = target.credentials
        spec = CredentialRefreshSpec(creds.harness, creds.token_refs, creds.guest_dir)
        try:
            fresh = get_provider(creds.harness).refresh_credentials(spec, target.bottle)
        except HostLoginError as e:
            if self._warn(target, str(e)):
                self._record(target, "credential.renew_failed", reason=str(e))
            return {"error": str(e)}
        changed = sorted(ref for ref, value in fresh.values.items()
                         if target.pushed.get(ref) != value)
        if changed:
            if not target.env_var_secret:
                target.env_var_secret = read_env_var_secret(target.bottle)
            for ref in changed:
                for slot in creds.slots.get(ref, ()):
                    self._push(target.registry_id, slot, fresh.values[ref],
                               target.env_var_secret)
                target.pushed[ref] = fresh.values[ref]
            log.info(f"bottle {target.identity}: refreshed its {creds.harness} login"
                     + (f" (valid until {fresh.expires_at:%Y-%m-%d %H:%M} UTC)"
                        if fresh.expires_at else ""))
            self._record(target, "credential.renewed", refs=changed,
                         expires_at=fresh.expires_at.isoformat() if fresh.expires_at else "")
        if fresh.expires_at is not None and fresh.expires_at - self._clock() < WARN_BEFORE:
            self._warn(target, f"the host's {creds.harness} login expires at "
                               f"{fresh.expires_at:%Y-%m-%d %H:%M} UTC; run `{creds.harness}` "
                               f"on the host to refresh it")
        else:
            target.warned = ""
        return {"refreshed": bool(changed),
                "expires_at": fresh.expires_at.isoformat() if fresh.expires_at else ""}

    def _warn(self, target: _Target, message: str) -> bool:
        """Warn once per distinct message, not every round; whether it's new."""
        if target.warned == message:
            return False
        target.warned = message
        log.warn(f"bottle {target.identity}: {message}")
        return True

    def _record(self, target: _Target, kind: str, **payload: object) -> None:
        record = getattr(self._audit, "credential_event", None)
        if record is not None:
            record(kind, bottle=target.identity, activation=target.registry_id,
                   harness=target.credentials.harness, **payload)


__all__ = ["CredentialRefresher", "REFRESH_SECONDS", "WARN_BEFORE", "read_env_var_secret"]
