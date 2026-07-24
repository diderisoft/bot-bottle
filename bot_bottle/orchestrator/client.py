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
from collections.abc import Iterable
from dataclasses import dataclass

from ..control_auth import ROLE_CLI, mint
from ..paths import host_control_plane_token
from .control_plane import CONTROL_AUTH_HEADER

DEFAULT_TIMEOUT_SECONDS = 5.0


def _host_auth_token() -> str:
    """A freshly minted `cli`-role token, signed with the host control-plane
    key, or "" if the key can't be read. The host CLI shares the orchestrator's
    trust domain, so it holds the signing key and mints its own operator token;
    "" means 'send no auth header' — correct against an open (unconfigured)
    control plane, and harmlessly rejected by a secured one."""
    try:
        return mint(ROLE_CLI, host_control_plane_token())
    except (OSError, ValueError):
        return ""


class OrchestratorClientError(RuntimeError):
    """A control-plane call failed (unreachable, or an unexpected status)."""


@dataclass(frozen=True)
class RegisteredBottle:
    """What `POST /bottles` returns: the minted bottle id and the per-bottle
    identity token the agent presents for app-layer attribution. `env_var_secret`
    is set by the caller (not from the server response) and carries the
    encryption key so it can be injected into the agent container's env."""

    bottle_id: str
    identity_token: str
    env_var_secret: str = ""


class OrchestratorClient:
    """Trusted host-side client for the orchestrator control plane.

    Presents the per-host control-plane secret on every call (the header the
    control plane requires on all routes but `/health`). The secret is read
    from the host file — this client only ever runs host-side (CLI, launcher,
    discovery), so it can read what an agent can't. `auth_token` is overridable
    for tests; the default reads the host file, minting it on first use."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        auth_token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._auth_token = auth_token if auth_token is not None else _host_auth_token()

    def _request(
        self, method: str, path: str, body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        """Send one request; return `(status, payload)`. Raises
        `OrchestratorClientError` only when the orchestrator can't be reached
        or returns malformed data — HTTP *status* codes are returned so
        callers can treat 404 as a meaningful "no such bottle"."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if self._auth_token:
            headers[CONTROL_AUTH_HEADER] = self._auth_token
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
        env_var_secret: str = "",
    ) -> RegisteredBottle:
        """Register a bottle and broker its launch (`POST /bottles`). `tokens`
        are the per-bottle egress auth values (env_name -> value) the
        orchestrator holds in memory for the gateway to inject. When
        *env_var_secret* is provided, the orchestrator also encrypts the token
        values and stores them in ``bottled_agent_secrets`` for restart
        recovery. Returns the minted id + identity token."""
        payload = self._ok("POST", "/bottles", {
            "source_ip": source_ip,
            "image_ref": image_ref,
            "metadata": metadata,
            "policy": policy,
            "tokens": tokens or {},
            "env_var_secret": env_var_secret,
        })
        bottle_id = payload.get("bottle_id")
        token = payload.get("identity_token")
        if not isinstance(bottle_id, str) or not isinstance(token, str):
            raise OrchestratorClientError("register: response missing bottle_id/identity_token")
        return RegisteredBottle(bottle_id=bottle_id, identity_token=token)

    def reprovision_gateway(self, bottle_id: str, env_var_secret: str) -> bool:
        """Re-inject a bottle's egress tokens from its ENV_VAR_SECRET
        (`POST /bottles/<id>/reprovision_gateway`). Returns True when the
        orchestrator successfully decrypted and restored the tokens, False
        when it had no stored secrets for this bottle (404)."""
        status, _ = self._request(
            "POST",
            f"/bottles/{bottle_id}/reprovision_gateway",
            {"env_var_secret": env_var_secret},
        )
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(
                f"reprovision_gateway {bottle_id}: HTTP {status}"
            )
        return True

    def teardown_bottle(self, bottle_id: str) -> bool:
        """Tear a bottle down (`DELETE /bottles/<id>`). False if the
        orchestrator didn't know it (404) — idempotent for cleanup paths."""
        status, _ = self._request("DELETE", f"/bottles/{bottle_id}")
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(f"teardown {bottle_id}: HTTP {status}")
        return True

    def reconcile(
        self, live_source_ips: Iterable[str], *, grace_seconds: float | None = None,
    ) -> list[str]:
        """Drop registry rows for bottles that are no longer running
        (`POST /reconcile`), returning the reaped bottle ids. `live_source_ips`
        is the caller's enumeration of its live bottles — the orchestrator
        can't see the backend from inside the infra container."""
        body: dict[str, object] = {"live_source_ips": list(live_source_ips)}
        if grace_seconds is not None:
            body["grace_seconds"] = grace_seconds
        payload = self._ok("POST", "/reconcile", body)
        reaped = payload.get("reaped")
        return [r for r in reaped if isinstance(r, str)] if isinstance(reaped, list) else []

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

    # --- supervise queue (operator TUI) ------------------------------------

    def supervise_pending(self) -> list[dict[str, object]]:
        """Pending supervise proposals across all bottles
        (`GET /supervise/proposals`)."""
        payload = self._ok("GET", "/supervise/proposals")
        proposals = payload.get("proposals")
        return proposals if isinstance(proposals, list) else []

    def supervise_respond(
        self,
        proposal_id: str,
        *,
        bottle_slug: str,
        decision: str,
        notes: str = "",
        final_file: str | None = None,
    ) -> None:
        """Record an operator decision (`POST /supervise/respond`). `decision`
        is approve/modify/reject. Raises `OrchestratorClientError` if the
        proposal is gone or the bottle can no longer be applied to (409)."""
        body: dict[str, object] = {
            "proposal_id": proposal_id,
            "bottle_slug": bottle_slug,
            "decision": decision,
            "notes": notes,
        }
        if final_file is not None:
            body["final_file"] = final_file
        self._ok("POST", "/supervise/respond", body)


def discover_orchestrator_url(*, timeout: float = 2.0) -> str:
    """The URL of the one running per-host orchestrator control plane, probing
    the backends' well-known control-plane addresses (both on port 8099):
    docker publishes it on loopback; the firecracker infra VM serves it on the
    orchestrator TAP. Returns the first that answers `/health`; raises if none
    do (no orchestrator up — launch a bottle first)."""
    candidates: list[str] = []
    try:  # docker: loopback-published control plane
        from .lifecycle import DEFAULT_PORT as _DOCKER_PORT
        candidates.append(f"http://127.0.0.1:{_DOCKER_PORT}")
    except Exception:  # noqa: BLE001 — backend optional
        candidates.append("http://127.0.0.1:8099")
    try:  # firecracker: infra VM control plane on the orchestrator TAP
        from ..backend.firecracker import netpool
        from ..backend.firecracker.infra_vm import CONTROL_PLANE_PORT
        candidates.append(
            f"http://{netpool.orch_slot().guest_ip}:{CONTROL_PLANE_PORT}")
    except Exception:  # noqa: BLE001 — backend optional / not firecracker
        pass
    try:  # macOS: infra container control plane on its host-only address
        from ..backend.macos_container.infra import probe_control_plane_url
        url = probe_control_plane_url()
        if url:
            candidates.append(url)
    except Exception:  # noqa: BLE001 — backend optional / not macOS
        pass
    for url in candidates:
        if OrchestratorClient(url, timeout=timeout).health():
            return url
    raise OrchestratorClientError(
        "no running orchestrator control plane found (tried "
        + ", ".join(candidates)
        + "); launch a bottle first"
    )


__all__ = [
    "OrchestratorClient",
    "OrchestratorClientError",
    "RegisteredBottle",
    "DEFAULT_TIMEOUT_SECONDS",
    "discover_orchestrator_url",
]
