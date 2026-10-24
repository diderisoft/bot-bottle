"""Per-request policy resolution for the shared gateway: `POST /resolve` on the
orchestrator, keyed by `(source_ip, identity_token)`.

No cache, so revocations, policy changes and teardowns apply immediately. An
unattributed client (403) resolves to None and the caller denies; orchestrator
errors raise so callers fail closed. The policy blob is returned as stored.
Stdlib only, no bot-bottle imports.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 2.0

# The `gateway` token this data plane sends on every control-plane call, from
# the env. Duplicated rather than imported to keep this module import-free.
ORCHESTRATOR_AUTH_HEADER = "x-bot-bottle-orchestrator-auth"
ORCHESTRATOR_AUTH_JWT_ENV = "BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT"


def orchestrator_auth_headers() -> dict[str, str]:
    """The auth header, or {} without a configured token."""
    token = os.environ.get(ORCHESTRATOR_AUTH_JWT_ENV, "").strip()
    return {ORCHESTRATOR_AUTH_HEADER: token} if token else {}


class PolicyResolveError(RuntimeError):
    """The orchestrator was unreachable or answered unexpectedly; a clean 403
    returns None instead."""


class PolicyResolver:
    """Resolves each client's policy from the orchestrator, per call."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object] | None:
        """POST `payload` to `path` and return the JSON object, or None on 403.
        Raises `PolicyResolveError` on anything else unexpected."""
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json", **orchestrator_auth_headers()},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return None  # unattributed: caller denies
            raise PolicyResolveError(f"{path} returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise PolicyResolveError(f"{path} unreachable or malformed: {e}") from e
        return data if isinstance(data, dict) else {}

    def _post_resolve(self, source_ip: str, identity_token: str) -> dict[str, object] | None:
        """The `/resolve` payload for this client, or None if unattributed.
        Raises `PolicyResolveError`."""
        return self._post_json(
            "/resolve", {"source_ip": source_ip, "identity_token": identity_token},
        )

    def propose_supervise(
        self,
        source_ip: str,
        identity_token: str,
        *,
        tool: str,
        proposed_file: str,
        justification: str,
    ) -> str | None:
        """Queue a supervise proposal and return its id. Attributed by
        `(source_ip, identity_token)`, so a bottle can only queue its own. None
        when unattributed; raises `PolicyResolveError` if unreachable."""
        payload = self._post_json("/supervise/propose", {
            "source_ip": source_ip,
            "identity_token": identity_token,
            "tool": tool,
            "proposed_file": proposed_file,
            "justification": justification,
        })
        if payload is None:
            return None
        proposal_id = payload.get("proposal_id")
        return proposal_id if isinstance(proposal_id, str) and proposal_id else None

    def poll_supervise(
        self, source_ip: str, identity_token: str, proposal_id: str,
    ) -> dict[str, object] | None:
        """Non-blocking poll of a proposal, attributed like
        `propose_supervise`. Returns `{"status": ...}`: a decision (with
        `notes` and `final_file`), `pending` or `unknown`. None when
        unattributed; raises if unreachable."""
        return self._post_json("/supervise/poll", {
            "source_ip": source_ip,
            "identity_token": identity_token,
            "proposal_id": proposal_id,
        })

    def resolve(self, source_ip: str, identity_token: str = "") -> str | None:
        """The calling bottle's policy blob, or None if unattributed. Raises
        `PolicyResolveError` if unreachable."""
        payload = self._post_resolve(source_ip, identity_token)
        if payload is None:
            return None
        policy = payload.get("policy")
        return policy if isinstance(policy, str) else ""

    def resolve_bottle_id(self, source_ip: str, identity_token: str = "") -> str | None:
        """The calling bottle's id (git-gate's namespace), or None if
        unattributed."""
        payload = self._post_resolve(source_ip, identity_token)
        if payload is None:
            return None
        bottle_id = payload.get("bottle_id")
        return bottle_id if isinstance(bottle_id, str) and bottle_id else None

    def resolve_audit_delivery(self, source_ip: str, identity_token: str = "") -> str | None:
        """The calling bottle's `audit.delivery`, or None if unattributed."""
        payload = self._post_resolve(source_ip, identity_token)
        if payload is None:
            return None
        return "required" if payload.get("audit_delivery") == "required" else "best_effort"

    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = "",
    ) -> tuple[str | None, str | None, dict[str, str]]:
        """Policy blob, bottle id and egress tokens in one `/resolve`. `(None,
        None, {})` when unattributed; raises if unreachable."""
        payload = self._post_resolve(source_ip, identity_token)
        if payload is None:
            return None, None, {}
        policy = payload.get("policy")
        bottle_id = payload.get("bottle_id")
        raw = payload.get("tokens")
        tokens = {
            k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw, dict) else {}
        return (
            policy if isinstance(policy, str) else "",
            bottle_id if isinstance(bottle_id, str) and bottle_id else None,
            tokens,
        )


__all__ = ["PolicyResolver", "PolicyResolveError"]
