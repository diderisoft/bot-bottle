"""Gateway-side per-client policy resolver.

The consolidated gateway serves every bottle from one process, so for each
request it must apply the *calling* bottle's policy, selected by the source
IP the attribution invariant makes unspoofable. This resolves that policy
from the orchestrator's control plane (`POST /resolve`), keyed on the
`(source_ip, identity_token)` pair.

**Always fresh — no cache.** The resolver is called rarely enough that a
round-trip doesn't matter, and correctness matters more than speed: every
resolve reflects the orchestrator's *current* view, so a revocation, a
policy change, or a bottle teardown the orchestrator knows about is honored
immediately rather than lingering for a cache TTL. (If this ever becomes a
hot path, add caching with orchestrator-driven invalidation — not a blind
TTL.)

**Fail-closed:** an unattributed client (the orchestrator answers `403`)
resolves to `None`, and the caller (the egress addon, git-gate) must then
deny — exactly as an unknown bottle should be treated. Orchestrator
*errors* (unreachable / unexpected status) raise, so the caller can fail
closed too rather than silently serving stale or empty policy.

The resolved value is the policy blob the orchestrator stores verbatim; the
consumer parses it (e.g. the egress addon's `load_config`). This module is
stdlib-only and free of bot-bottle imports.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 2.0

# The role-scoped `gateway` token this data plane presents on every control-plane
# call, read from the env the launcher injects into the gateway (a pre-minted
# signed JWT — the gateway never holds the signing key, so it can't forge a
# higher-privilege `cli` token). Constant + env-var name are duplicated here
# rather than imported because this module is COPYed flat into the gateway image,
# free of bot-bottle imports — same rationale as IDENTITY_HEADER in egress_addon
# / git_http_backend.
ORCHESTRATOR_AUTH_HEADER = "x-bot-bottle-orchestrator-auth"
ORCHESTRATOR_AUTH_JWT_ENV = "BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT"


def _orchestrator_auth_headers() -> dict[str, str]:
    """The auth header to send, or {} when no token is configured (an open
    control plane, e.g. Firecracker behind its nft boundary — sending nothing
    is correct there and harmlessly ignored)."""
    token = os.environ.get(ORCHESTRATOR_AUTH_JWT_ENV, "").strip()
    return {ORCHESTRATOR_AUTH_HEADER: token} if token else {}


class PolicyResolveError(RuntimeError):
    """The orchestrator was unreachable or returned an unexpected status —
    distinct from a clean `403` (unattributed), which returns None."""


class PolicyResolver:
    """Resolves each client's policy from the orchestrator, fresh per call."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object] | None:
        """POST `payload` to control-plane `path`, returning the JSON object body
        — or None when the orchestrator answers `403` (unattributed / fail
        closed). Raises `PolicyResolveError` on an unreachable / unexpected-status
        / malformed response so every caller can fail closed. Shared by
        `_post_resolve` and the supervise propose/poll RPCs."""
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json", **_orchestrator_auth_headers()},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return None  # unattributed → fail closed (caller denies)
            raise PolicyResolveError(f"{path} returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise PolicyResolveError(f"{path} unreachable or malformed: {e}") from e
        return data if isinstance(data, dict) else {}

    def _post_resolve(self, source_ip: str, identity_token: str) -> dict[str, object] | None:
        """The orchestrator's `/resolve` payload for this client, or None if
        unattributed (a clean `403`). Raises `PolicyResolveError` on an
        unreachable / unexpected-status / malformed response so every caller
        can fail closed. Shared by `resolve` and `resolve_bottle_id`."""
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
        """Queue a supervise proposal on the control plane and return its
        `proposal_id`. The orchestrator attributes the proposal to the calling
        bottle by `(source_ip, identity_token)` — exactly like `/resolve` — so a
        bottle can only ever queue its *own* proposals. Returns None when the
        pair is unattributed (a clean `403`); raises `PolicyResolveError` if the
        orchestrator can't be reached, so the data-plane caller fails closed
        (blocks / refuses) rather than silently dropping the proposal."""
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
        """Poll a queued proposal for the operator's decision, **non-blocking**.
        Attributed by `(source_ip, identity_token)` so a bottle can only read its
        *own* proposal's response. Returns `{"status": ...}` where status is one
        of the terminal decisions (`approved`/`modified`/`rejected`, carrying
        `notes` + `final_file`), `pending` (queued, no decision yet), or
        `unknown` (no such queued proposal for this bottle). Returns None when
        unattributed; raises `PolicyResolveError` if unreachable."""
        return self._post_json("/supervise/poll", {
            "source_ip": source_ip,
            "identity_token": identity_token,
            "proposal_id": proposal_id,
        })

    def resolve(self, source_ip: str, identity_token: str = "") -> str | None:
        """The calling bottle's policy blob, or None if unattributed. Always
        fetches from the orchestrator so revocations / changes / teardowns
        are honored immediately.

        `identity_token` is optional *transitionally* — omitting it resolves
        by source IP alone (the network-layer attribution, sound by
        construction on Firecracker's `/31`+nft, weaker elsewhere). The
        consolidated end state makes the token mandatory so the app-layer
        defense is always enforced, not silently degraded; that flips at the
        `/resolve` boundary once identity-token *delivery* lands (we can't
        require what the agent can't yet present).
        Raises `PolicyResolveError` if the orchestrator can't be reached."""
        payload = self._post_resolve(source_ip, identity_token)
        if payload is None:
            return None
        policy = payload.get("policy")
        return policy if isinstance(policy, str) else ""

    def resolve_bottle_id(self, source_ip: str, identity_token: str = "") -> str | None:
        """The calling bottle's id, or None if unattributed. This is the
        source-IP-keyed identity the git-gate uses to select the bottle's
        repo namespace (there is no per-bottle policy blob to parse — the
        bottle *is* the namespace). Same fail-closed contract as `resolve`."""
        payload = self._post_resolve(source_ip, identity_token)
        if payload is None:
            return None
        bottle_id = payload.get("bottle_id")
        return bottle_id if isinstance(bottle_id, str) and bottle_id else None

    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = "",
    ) -> tuple[str | None, str | None, dict[str, str]]:
        """The policy blob, bottle id, **and per-bottle egress auth tokens** in
        a single `/resolve` — so the egress addon gets everything it needs
        (policy for routing, bottle id for the supervise queue/safelist, tokens
        to inject upstream auth) in one round-trip. Returns `(None, None, {})`
        when unattributed (a clean `403`). Raises `PolicyResolveError` if the
        orchestrator can't be reached, so the caller still fails closed."""
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
