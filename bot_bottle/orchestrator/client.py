"""Host-side control-plane client (PRD 0070).

The launch path talks to the orchestrator over its HTTP control plane to
register, re-policy, and tear down bottles — the counterpart to the
gateway-side `PolicyResolver` (which only reads `/resolve`). Where
`PolicyResolver` is fail-closed and lives in the untrusted data plane, this
is the trusted control-plane caller: a non-success response is an error the
launch path must surface, not silently swallow.

Stdlib-only, so the CLI can drive the orchestrator without any dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 5.0


class OrchestratorClientError(RuntimeError):
    """A control-plane call failed (unreachable, or an unexpected status)."""


@dataclass(frozen=True)
class RegisteredBottle:
    """What `POST /bottles` returns: the minted bottle id and the per-bottle
    identity token the agent presents for app-layer attribution."""

    bottle_id: str
    identity_token: str


class OrchestratorClient:
    """Trusted host-side client for the orchestrator control plane."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _request(
        self, method: str, path: str, body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        """Send one request; return `(status, payload)`. Raises
        `OrchestratorClientError` only when the orchestrator can't be reached
        or returns malformed data — HTTP *status* codes are returned so
        callers can treat 404 as a meaningful "no such bottle"."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(
            f"{self._base}{path}", data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                payload = json.loads(raw) if raw else {}
                return resp.status, payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as e:
            # A structured error response still carries a usable status.
            try:
                payload = json.loads(e.read() or b"{}")
            except (ValueError, OSError):
                payload = {}
            return e.code, payload if isinstance(payload, dict) else {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise OrchestratorClientError(f"{method} {path}: {e}") from e

    def _ok(self, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        """`_request` that requires a 2xx, raising otherwise."""
        status, payload = self._request(method, path, body)
        if not 200 <= status < 300:
            detail = payload.get("error", "")
            raise OrchestratorClientError(f"{method} {path}: HTTP {status} {detail}".rstrip())
        return payload

    def health(self) -> bool:
        """True iff the control plane answers `GET /health` with 200."""
        try:
            status, _ = self._request("GET", "/health")
        except OrchestratorClientError:
            return False
        return status == 200

    def register_bottle(
        self,
        source_ip: str,
        *,
        image_ref: str = "",
        metadata: str = "",
        policy: str = "",
        tokens: dict[str, str] | None = None,
    ) -> RegisteredBottle:
        """Register a bottle and broker its launch (`POST /bottles`). `tokens`
        are the per-bottle egress auth values (env_name -> value) the
        orchestrator holds in memory for the gateway to inject. Returns the
        minted id + identity token."""
        payload = self._ok("POST", "/bottles", {
            "source_ip": source_ip,
            "image_ref": image_ref,
            "metadata": metadata,
            "policy": policy,
            "tokens": tokens or {},
        })
        bottle_id = payload.get("bottle_id")
        token = payload.get("identity_token")
        if not isinstance(bottle_id, str) or not isinstance(token, str):
            raise OrchestratorClientError("register: response missing bottle_id/identity_token")
        return RegisteredBottle(bottle_id=bottle_id, identity_token=token)

    def teardown_bottle(self, bottle_id: str) -> bool:
        """Tear a bottle down (`DELETE /bottles/<id>`). False if the
        orchestrator didn't know it (404) — idempotent for cleanup paths."""
        status, _ = self._request("DELETE", f"/bottles/{bottle_id}")
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(f"teardown {bottle_id}: HTTP {status}")
        return True

    def set_policy(self, bottle_id: str, policy: str) -> bool:
        """Live-reload a bottle's policy (`PUT /bottles/<id>/policy`). False on
        404 (unknown bottle)."""
        status, _ = self._request("PUT", f"/bottles/{bottle_id}/policy", {"policy": policy})
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(f"set_policy {bottle_id}: HTTP {status}")
        return True

    def list_bottles(self) -> list[dict[str, object]]:
        """Every registered bottle's redacted record (`GET /bottles`)."""
        payload = self._ok("GET", "/bottles")
        bottles = payload.get("bottles")
        return bottles if isinstance(bottles, list) else []


__all__ = [
    "OrchestratorClient",
    "OrchestratorClientError",
    "RegisteredBottle",
    "DEFAULT_TIMEOUT_SECONDS",
]
