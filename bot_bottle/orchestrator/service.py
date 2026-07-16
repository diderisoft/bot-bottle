"""The per-host orchestrator core (PRD 0070).

`Orchestrator` is the single backend-neutral object the control plane talks
to: it owns the registry (runtime state) and brokers agent launches. It
never branches on backend — the `LaunchBroker` abstracts the backend-native
launch, so this same object drives docker / firecracker / apple once a real
broker is wired in.

Launch lifecycle:

  * `launch_bottle` mints the bottle (registry: source IP + identity
    token), sends a *signed, structured* launch request through the broker,
    and returns the record. If the broker rejects/fails, the registry entry
    is rolled back so a failed launch leaves no orphan.
  * `teardown_bottle` sends a signed teardown request, then deregisters.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .broker import LaunchBroker, LaunchRequest, sign_request
from .registry import BottleRecord, RegistryStore
from .gateway import Gateway
from ..supervise import (
    AuditEntry,
    COMPONENT_FOR_TOOL,
    Response,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    list_all_pending_proposals,
    read_proposal,
    render_diff,
    write_audit_entry,
    write_response,
)


# Operator decision → Response.status. The apply half (egress tools) runs
# for approve/modify only.
_RESPOND_STATUS = {
    "approve": STATUS_APPROVED,
    "modify": STATUS_MODIFIED,
    "reject": STATUS_REJECTED,
}
_APPLY_TOOLS = (TOOL_EGRESS_ALLOW, TOOL_EGRESS_BLOCK)


class Orchestrator:
    """Owns the registry + brokers launches, and manages the single
    consolidated per-host gateway. Backend-neutral (broker and gateway
    abstract the backend-native pieces)."""

    def __init__(
        self,
        registry: RegistryStore,
        broker: LaunchBroker,
        sign_secret: bytes,
        gateway: Gateway | None = None,
    ) -> None:
        self.registry = registry
        self._broker = broker
        self._secret = sign_secret
        self._gateway = gateway
        # Per-bottle egress auth tokens (env_name -> value), keyed by bottle_id.
        # Held **in memory only** — never written to the registry DB — so the
        # gateway can inject each bottle's upstream credential without secrets
        # at rest. Lost on restart (re-launch re-registers them); the future
        # SecretProvider (#355) replaces this with per-request minting.
        self._tokens: dict[str, dict[str, str]] = {}

    def launch_bottle(
        self,
        source_ip: str,
        *,
        image_ref: str = "",
        slot: int | None = None,
        metadata: str = "",
        policy: str = "",
        tokens: dict[str, str] | None = None,
    ) -> BottleRecord:
        """Register a bottle (with its gateway policy + in-memory egress auth
        tokens) and broker its launch. Rolls the registry entry back if the
        launch doesn't take, so a failure leaves no orphan."""
        rec = self.registry.register(source_ip, metadata=metadata, policy=policy)
        if tokens:
            self._tokens[rec.bottle_id] = dict(tokens)
        req = LaunchRequest(
            op="launch",
            bottle_id=rec.bottle_id,
            source_ip=source_ip,
            image_ref=image_ref,
            slot=slot,
        )
        launched = False
        try:
            self._broker.submit(sign_request(req, self._secret))
            launched = True
        finally:
            if not launched:
                self.registry.deregister(rec.bottle_id)
                self._tokens.pop(rec.bottle_id, None)
        return rec

    def teardown_bottle(self, bottle_id: str) -> bool:
        """Broker teardown then deregister. False if the bottle is unknown."""
        rec = self.registry.get(bottle_id)
        if rec is None:
            return False
        req = LaunchRequest(op="teardown", bottle_id=bottle_id, source_ip=rec.source_ip)
        self._broker.submit(sign_request(req, self._secret))
        self.registry.deregister(bottle_id)
        self._tokens.pop(bottle_id, None)
        return True

    def tokens_for(self, bottle_id: str) -> dict[str, str]:
        """The bottle's in-memory egress auth tokens (env_name -> value), or
        empty. The gateway injects these per request; they are never
        persisted."""
        return dict(self._tokens.get(bottle_id, {}))

    def attribute(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """Fail-closed attribution (delegates to the registry)."""
        return self.registry.attribute(source_ip, identity_token)

    def resolve(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """Resolve the bottle behind a request — the per-request lookup the
        multi-tenant gateway makes; the returned record carries its `policy`.

        **Mandatory pair**: requires a matching `(source_ip, identity_token)`
        (constant-time). There is no source-IP-only fallback — the app-layer
        token is delivered on every attributed data plane (egress proxy
        credentials, git-gate/supervise headers), so a missing or mismatched
        token fail-closes. This keeps a spoofed source IP (which the /31 TAP
        alone does not prevent) from selecting another bottle's policy/tokens
        without also holding that bottle's unguessable token."""
        return self.registry.attribute(source_ip, identity_token)

    def set_policy(self, bottle_id: str, policy: str) -> bool:
        """Update a bottle's gateway policy in place (live reload). False if
        the bottle is unknown."""
        return self.registry.set_policy(bottle_id, policy)

    # --- supervise queue (operator approvals) ------------------------------
    #
    # The orchestrator owns the single DB *and* the live policy, so operator
    # decisions are applied here, server-side, and reached over HTTP by the
    # host TUI (no direct-DB access, one path for every backend).

    def supervise_pending(self) -> list[dict[str, object]]:
        """All pending proposals across bottles, FIFO, as JSON dicts
        (`Proposal.to_dict`, round-trippable via `Proposal.from_dict`)."""
        return [p.to_dict() for p in list_all_pending_proposals()]

    def _record_for_slug(self, slug: str) -> BottleRecord | None:
        """The live registry record whose metadata carries `slug`, or None
        (e.g. the bottle was torn down before the operator responded)."""
        for rec in self.registry.all():
            try:
                meta = json.loads(rec.metadata) if rec.metadata else {}
            except ValueError:
                meta = {}
            if isinstance(meta, dict) and meta.get("slug") == slug:
                return rec
        return None

    def supervise_respond(
        self,
        proposal_id: str,
        *,
        bottle_slug: str,
        decision: str,
        notes: str = "",
        final_file: str | None = None,
    ) -> tuple[bool, str]:
        """Record an operator decision on a queued proposal, applying it
        server-side. `decision` is approve/modify/reject.

        Approve/modify on an egress tool rewrites the bottle's policy so the
        gateway serves the new routes on its next `/resolve` (the live apply);
        then the queued Response is written (unblocking the agent's MCP call)
        and an audit entry recorded — all against the one DB. Returns
        (ok, error): ok=False with a message when the proposal or decision is
        unknown, or the bottle is gone so an approval can't be applied."""
        status = _RESPOND_STATUS.get(decision)
        if status is None:
            return False, f"unknown decision {decision!r}"
        try:
            proposal = read_proposal(bottle_slug, proposal_id)
        except FileNotFoundError:
            return False, "no such proposal"

        diff_before, diff_after = "", ""
        if status in (STATUS_APPROVED, STATUS_MODIFIED) and proposal.tool in _APPLY_TOOLS:
            new_policy = final_file if final_file is not None else proposal.proposed_file
            rec = self._record_for_slug(bottle_slug)
            if rec is None:
                return False, (
                    f"bottle {bottle_slug!r} is no longer registered; "
                    "cannot apply the route change"
                )
            diff_before, diff_after = rec.policy, new_policy
            self.set_policy(rec.bottle_id, new_policy)

        write_response(bottle_slug, Response(
            proposal_id=proposal_id, status=status, notes=notes, final_file=final_file,
        ))
        component = COMPONENT_FOR_TOOL.get(proposal.tool)
        if component is not None:
            write_audit_entry(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                bottle_slug=bottle_slug,
                component=component,
                operator_action=status,
                operator_notes=notes,
                justification=proposal.justification,
                diff=render_diff(diff_before, diff_after, label=component),
            ))
        return True, ""

    # --- consolidated gateway ----------------------------------------------

    def ensure_gateway(self) -> None:
        """Ensure the single per-host gateway is built and up (idempotent).
        No-op when no gateway is configured."""
        if self._gateway is not None:
            self._gateway.ensure_built()
            self._gateway.ensure_running()

    def gateway_status(self) -> dict[str, object]:
        """Report the shared gateway for the control plane / console."""
        if self._gateway is None:
            return {"configured": False}
        return {
            "configured": True,
            "name": self._gateway.name,
            "running": self._gateway.is_running(),
        }


__all__ = ["Orchestrator"]
