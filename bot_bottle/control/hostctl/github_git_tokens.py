"""Keep the GitHub token git-gate uses for HTTPS repos current (ADR 0014).

A GitHub user access token lasts eight hours; a bottle may run longer. Every
few minutes this renews each running bottle's token through
`user_token.current_token` (which renews it within ten minutes of expiry) and,
when it changed, pushes the new one into the gateway's tmpfs
(`provision_gateway.push_tokens`). The host owns renewal; the gateway only
ever holds the current token, in memory. An authorization that can't be
renewed (ReauthorizeError) raises a console alert (`forge_alerts`) instead of
a warning every pass; network trouble stays a warning and is retried.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any

from ... import forge_alerts
from ...backend import adoption
from ...backend.provision_gateway import github_token, github_token_expires, push_tokens
from ...contrib.github.user_token import ReauthorizeError
from ...git_gate.provision import saved_upstreams
from ...lib import log

# Shorter than the ten minutes `current_token` renews ahead of expiry, so a
# renewed token reaches the gateway before the old one lapses.
RENEW_SECONDS = 240.0


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class GitTokenRenewer:
    """Pushes each running bottle's renewed GitHub token to its gateway."""

    def __init__(self, bottles: Any, *,
                 token_for: Callable[[str], str | None] = github_token,
                 transport_for: Callable[[str], Any] | None = None,
                 expires_for: Callable[[str], int | None] = github_token_expires) -> None:
        self._bottles = bottles
        self._token_for = token_for
        self._expires_for = expires_for
        self._transport_for = transport_for or self._gateway_transport
        # (bottle, secret) -> digest of the token last pushed.
        self._pushed: dict[tuple[str, str], str] = {}
        self._stop = threading.Event()

    def _gateway_transport(self, identity: str) -> Any:
        backend = self._bottles.backend_for_session(identity)
        if backend is None:
            return None
        return backend.infra_service().gateway().provisioning_transport()

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(RENEW_SECONDS):
                self.renew_all()
        threading.Thread(target=loop, daemon=True, name="hostctl-github-git-tokens").start()

    def stop(self) -> None:
        self._stop.set()

    def renew_all(self) -> list[str]:
        """One pass: the bottles whose gateway got a new token. Never raises."""
        pushed: list[str] = []
        for record in self._bottles.sessions.records():
            try:
                if self._renew(record.id):
                    pushed.append(record.id)
            except Exception as e:  # pylint: disable=broad-exception-caught  # next pass
                log.warn(f"bottle {record.id}: couldn't renew its GitHub token for "
                         f"git-gate: {e}")
        return pushed

    def _renew(self, identity: str) -> bool:
        upstreams = tuple(u for u in saved_upstreams(identity)
                          if u.transport == "https" and u.token_secret)
        if not upstreams:
            return False
        tokens: dict[str, str] = {}
        for secret in {u.token_secret for u in upstreams}:
            try:
                token = self._token_for(secret)
            except ReauthorizeError as e:
                # For good: the console shows it once; warned only when new.
                if forge_alerts.raise_alert(secret, kind=forge_alerts.GITHUB_REAUTHORIZE,
                                            forge="github.com", message=str(e)):
                    log.warn(f"bottle {identity}: git-gate's GitHub token can't be "
                             f"renewed: {e}")
                continue
            if token:
                tokens[secret] = token
                forge_alerts.clear(secret)
        changed = {s for s, t in tokens.items()
                   if self._pushed.get((identity, s)) != _digest(t)}
        if not changed:
            return False
        adopted = adoption.read(identity)
        transport = self._transport_for(identity)
        if adopted is None or not adopted.registry_id or transport is None:
            return False
        push_tokens(transport, adopted.registry_id, upstreams,
                    token_for=tokens.get, expires_for=self._expires_for)
        for secret in changed:
            self._pushed[(identity, secret)] = _digest(tokens[secret])
        return True


__all__ = ["RENEW_SECONDS", "GitTokenRenewer"]
