"""Gateway-side per-client policy resolver (PRD 0070).

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
stdlib-only and free of bot-bottle imports so it can be COPYed flat into
the sidecar bundle.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 2.0


class PolicyResolveError(RuntimeError):
    """The orchestrator was unreachable or returned an unexpected status —
    distinct from a clean `403` (unattributed), which returns None."""


class PolicyResolver:
    """Resolves each client's policy from the orchestrator, fresh per call."""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _post_resolve(self, source_ip: str, identity_token: str) -> dict[str, object] | None:
        """The orchestrator's `/resolve` payload for this client, or None if
        unattributed (a clean `403`). Raises `PolicyResolveError` on an
        unreachable / unexpected-status / malformed response so every caller
        can fail closed. Shared by `resolve` and `resolve_bottle_id`."""
        body = json.dumps(
            {"source_ip": source_ip, "identity_token": identity_token}
        ).encode()
        req = urllib.request.Request(
            f"{self._base}/resolve", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                return None  # unattributed → fail closed (caller denies)
            raise PolicyResolveError(f"/resolve returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise PolicyResolveError(f"/resolve unreachable or malformed: {e}") from e
        return payload if isinstance(payload, dict) else {}

    def resolve(self, source_ip: str, identity_token: str = "") -> str | None:
        """The calling bottle's policy blob, or None if unattributed. Always
        fetches from the orchestrator so revocations / changes / teardowns
        are honored immediately.

        `identity_token` is optional *transitionally* — omitting it resolves
        by source IP alone (the network-layer attribution, sound by
        construction on Firecracker's `/31`+nft, weaker elsewhere). The
        consolidated end state makes the token mandatory so the app-layer
        defense is always enforced, not silently degraded; that flips at the
        `/resolve` boundary once identity-token *delivery* lands (a PRD 0070
        open question — we can't require what the agent can't yet present).
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


__all__ = ["PolicyResolver", "PolicyResolveError"]
