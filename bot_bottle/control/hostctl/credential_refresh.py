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

The host's CLI revokes the old access token the moment it rotates its login,
so waiting for the timer leaves bottles on a revoked token (401s) for up to
REFRESH_SECONDS. So the host controller also watches the files each provider
names (`host_login_paths`) every WATCH_SECONDS, and refreshes the bottles
forwarding that harness's login as soon as one changes and reads whole (a
half-written file waits for the next look). The timer stays, for logins kept
outside a file (the macOS Keychain) and as a backstop.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ...errors import HostLoginError
from ...lib import log

if TYPE_CHECKING:
    from ...backend import Bottle
    from ...backend.adoption import AdoptedCredentials

REFRESH_SECONDS = 300.0
# How often the host login files are looked at for a rotation.
WATCH_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 60.0  # reading the bottle's secret from inside it
# How long before the host login expires to start warning.
WARN_BEFORE = timedelta(hours=6)
# Where firecracker keeps the bottle's ENV_VAR_SECRET; the container backends
# put it in the agent's environment.
_ENV_VAR_SECRET_FILE = "/run/bot-bottle/env-var-secret"

# (registry id, egress slot, value, env_var_secret) -> pushed, or raises.
Push = Callable[[str, str, str, str], None]
# A host login's files -> what identifies their current contents (None: absent).
Signature = tuple[tuple[int, int] | None, ...]


def _signature(paths: tuple[Path, ...]) -> Signature:
    out: list[tuple[int, int] | None] = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            out.append(None)
        else:
            out.append((st.st_mtime_ns, st.st_size))
    return tuple(out)


def _whole(paths: tuple[Path, ...]) -> bool:
    """Whether every present file reads as JSON: the CLI rewrites them, and a
    look mid-write mustn't count as a (broken) new login."""
    for path in paths:
        try:
            json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return False
    return True


def _login_paths(harness: str) -> tuple[Path, ...]:
    # pylint: disable-next=import-outside-toplevel
    from ...agent_provider import get_provider
    return get_provider(harness).host_login_paths()


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
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 login_paths: Callable[[str], tuple[Path, ...]] = _login_paths) -> None:
        self._push = push
        self._audit = audit
        self._clock = clock
        self._login_paths = login_paths
        self._targets: dict[str, _Target] = {}
        self._lock = threading.Lock()
        # One refresh at a time: the timer, the watch and the console's
        # Refresh credentials share each target's `pushed`.
        self._refreshing = threading.Lock()
        # Each harness's login files as last seen whole, to spot a rotation.
        self._seen: dict[str, Signature] = {}
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

        def watch() -> None:
            while not self._stop.wait(WATCH_SECONDS):
                try:
                    self.watch_once()
                except Exception as e:  # pylint: disable=broad-exception-caught  # keep watching
                    log.warn(f"host controller: watching the host logins failed: {e}")
        threading.Thread(target=loop, daemon=True, name="hostctl-credentials").start()
        threading.Thread(target=watch, daemon=True, name="hostctl-credentials-watch").start()

    def watch_once(self) -> list[str]:
        """One look at the host login files of the harnesses running bottles
        forward: refresh, now, the bottles of each harness whose files
        changed since last seen whole. The bottles refreshed."""
        with self._lock:
            targets = list(self._targets.values())
        refreshed: list[str] = []
        for harness in sorted({t.credentials.harness for t in targets}):
            paths = self._login_paths(harness)
            if not paths:
                continue
            signature = _signature(paths)
            before = self._seen.get(harness)
            if signature == before:
                continue
            if not _whole(paths):
                continue  # mid-write: look again next time
            self._seen[harness] = signature
            if before is None:
                continue  # first look: what the bottles launched with
            for target in targets:
                if target.credentials.harness != harness:
                    continue
                try:
                    self.refresh(target)
                except Exception as e:  # pylint: disable=broad-exception-caught  # one bottle mustn't stop the rest
                    self._warn(target, f"couldn't refresh its {harness} login: {e}")
                    continue
                refreshed.append(target.identity)
        return refreshed

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
        with self._refreshing:
            return self._refresh(target)

    def _refresh(self, target: _Target) -> dict[str, object]:
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


__all__ = ["CredentialRefresher", "REFRESH_SECONDS", "WARN_BEFORE", "WATCH_SECONDS",
           "read_env_var_secret"]
