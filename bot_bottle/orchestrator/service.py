"""`OrchestratorCore`: the backend-neutral object the control plane wraps. It
owns the registry and the supervise queue. The host controller registers each
bottle during launch; `reconcile` (on request and on a timer) removes rows for
bottles no longer running, since an orphan row on a recycled IP breaks the next
bottle there.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol

from ..contract.supervise import (
    AuditEntry,
    COMPONENT_FOR_TOOL,
    POLL_STATUS_PENDING,
    POLL_STATUS_UNKNOWN,
    Proposal,
    Response,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
)
from ..lib import log
from .hostctl_relay import LiveBottlesUnknown
from .store.registry_store import DEFAULT_REAP_GRACE_SECONDS, BottleRecord, RegistryStore
from .supervisor import Supervisor
from ..lib.text import render_diff


# Operator decision -> Response.status. Egress changes apply for approve and
# modify only.
_RESPOND_STATUS = {
    "approve": STATUS_APPROVED,
    "modify": STATUS_MODIFIED,
    "reject": STATUS_REJECTED,
}
_APPLY_TOOLS = (TOOL_EGRESS_ALLOW, TOOL_EGRESS_BLOCK)


class LiveSource(Protocol):
    """Where `reconcile` learns which bottles run (`HostControlRelay`)."""

    def live_source_ips(self) -> list[str]: ...


class OrchestratorCore:
    """Owns the bottle registry and the supervise queue."""

    def __init__(
        self,
        registry: RegistryStore,
        supervisor: Supervisor | None = None,
    ) -> None:
        self.registry = registry
        # Injectable for tests; defaults to the registry's DB.
        self._supervisor = supervisor or Supervisor(registry.db_path)
        # Per-bottle egress tokens (env name -> value), in memory only;
        # encrypted copies live in the DB for restart recovery.
        self._tokens: dict[str, dict[str, str]] = {}

    def register_bottle(
        self,
        source_ip: str,
        *,
        metadata: str = "",
        policy: str = "",
        tokens: dict[str, str] | None = None,
        env_var_secret: str = "",
    ) -> BottleRecord:
        """Register a bottle with its policy and egress tokens. With
        `env_var_secret`, the tokens are also stored encrypted for
        `reprovision_from_secret`."""
        rec = self.registry.register(source_ip, metadata=metadata, policy=policy)
        if tokens:
            self._tokens[rec.bottle_id] = dict(tokens)
            if env_var_secret:
                from .store.secret_store import encrypt_value
                encrypted = {k: encrypt_value(env_var_secret, v) for k, v in tokens.items()}
                self.registry.store_agent_secrets(rec.bottle_id, encrypted)
        return rec

    def deregister_bottle(self, bottle_id: str) -> bool:
        """Drop a bottle's registration. False if the bottle is unknown."""
        if self.registry.get(bottle_id) is None:
            return False
        self.registry.deregister(bottle_id)
        self._tokens.pop(bottle_id, None)
        # Archive the bottle's unanswered proposals.
        self._supervisor.archive_all_proposals(bottle_id)
        return True

    def reconcile(
        self,
        hostctl: LiveSource | None,
        *,
        grace_seconds: float = DEFAULT_REAP_GRACE_SECONDS,
    ) -> list[str]:
        """Drop rows (and tokens) for bottles no longer running; returns the
        reaped ids. The live set comes from the host controller. Without a
        complete live set nothing is reaped and `LiveBottlesUnknown` is raised,
        since a missing answer must never read as "nothing running". See
        `RegistryStore.reap_absent`."""
        try:
            if hostctl is None:
                raise LiveBottlesUnknown("no host controller to ask")
            live = hostctl.live_source_ips()
        except LiveBottlesUnknown as e:
            log.warn(f"registry reconciliation skipped: {e}")
            raise
        reaped = self.registry.reap_absent(live, grace_seconds=grace_seconds)
        for rec in reaped:
            self._tokens.pop(rec.bottle_id, None)
            # Archive the bottle's unanswered proposals.
            self._supervisor.archive_all_proposals(rec.bottle_id)
        return [rec.bottle_id for rec in reaped]

    def tokens_for(self, bottle_id: str) -> dict[str, str]:
        """The bottle's in-memory egress tokens, or {}."""
        return dict(self._tokens.get(bottle_id, {}))

    def attribute(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """Fail-closed attribution (the registry's)."""
        return self.registry.attribute(source_ip, identity_token)

    def resolve(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """The bottle behind a request; the record carries its `policy`.
        Requires a matching `(source_ip, identity_token)` (constant-time), with
        no IP-only fallback, so a spoofed address alone selects nothing."""
        return self.registry.attribute(source_ip, identity_token)

    def set_policy(self, bottle_id: str, policy: str) -> bool:
        """Replace a bottle's policy in place. False if unknown."""
        return self.registry.set_policy(bottle_id, policy)

    # --- supervise queue ---
    #
    # Operator decisions and the agent side of the queue both go through here,
    # keyed by the orchestrator-resolved bottle id, so a bottle can only queue
    # and read its own proposals.

    def supervise_queue_proposal(
        self, bottle_id: str, *, tool: str, proposed_file: str, justification: str,
    ) -> str:
        """Queue a proposal under `bottle_id` (resolved by the control plane,
        not claimed by the caller) and return its id. The file hash is computed
        here."""
        proposal = Proposal.new(
            bottle_slug=bottle_id,
            tool=tool,
            proposed_file=proposed_file,
            justification=justification,
        )
        self._supervisor.write_proposal(proposal)
        return proposal.id

    def supervise_poll_response(self, bottle_id: str, proposal_id: str) -> dict[str, object]:
        """Non-blocking, idempotent poll of one of `bottle_id`'s proposals:
        `{"status": ...}` with a decision (plus `notes` and `final_file`),
        `pending`, or `unknown`. Polling doesn't archive, so a client that lost
        a reply gets it again. Scoped to `bottle_id`."""
        try:
            response = self._supervisor.read_response(bottle_id, proposal_id)
        except FileNotFoundError:
            try:
                self._supervisor.read_proposal(bottle_id, proposal_id)
            except FileNotFoundError:
                return {"status": POLL_STATUS_UNKNOWN}
            return {"status": POLL_STATUS_PENDING}
        return {
            "status": response.status,
            "notes": response.notes,
            "final_file": response.final_file,
        }

    def supervise_pending(self) -> list[dict[str, object]]:
        """Pending proposals across bottles, FIFO, as `Proposal.to_dict` dicts
        plus a `bottle_label` (the human slug) and, for egress tools, the
        `current_file` it would replace. Responses still use `bottle_slug`."""
        out: list[dict[str, object]] = []
        for p in self._supervisor.list_all_pending_proposals():
            d = p.to_dict()
            d["bottle_label"] = self._label_for(p.bottle_slug)
            if p.tool in _APPLY_TOOLS:
                # What the proposal would replace, so a reviewer sees just the
                # change.
                rec = self.registry.get(p.bottle_slug)
                d["current_file"] = rec.policy if rec is not None else ""
            out.append(d)
        return out

    def session_for(self, bottle_id: str) -> str:
        """The host controller's name for a bottle: its registered slug, or the
        id."""
        return self._label_for(bottle_id)

    def audit_delivery(self, bottle_id: str) -> str:
        """The bottle's `audit.delivery`, `best_effort` when unknown or unset."""
        rec = self.registry.get(bottle_id)
        try:
            meta = json.loads(rec.metadata) if rec is not None and rec.metadata else {}
        except ValueError:
            meta = {}
        value = meta.get("audit_delivery") if isinstance(meta, dict) else None
        return "required" if value == "required" else "best_effort"

    def _label_for(self, bottle_slug: str) -> str:
        """The proposal's bottle slug from registry metadata, else
        `bottle_slug` unchanged."""
        rec = self.registry.get(bottle_slug)
        if rec is None:
            return bottle_slug
        try:
            meta = json.loads(rec.metadata) if rec.metadata else {}
        except ValueError:
            meta = {}
        slug = meta.get("slug") if isinstance(meta, dict) else None
        return slug if isinstance(slug, str) and slug else bottle_slug

    def _record_for_slug(self, slug: str) -> BottleRecord | None:
        """The live record for a proposal's bottle (proposals are keyed by
        bottle id), or None if it's gone."""
        return self.registry.get(slug)

    def supervise_respond(
        self,
        proposal_id: str,
        *,
        bottle_slug: str,
        decision: str,
        notes: str = "",
        final_file: str | None = None,
    ) -> tuple[bool, str]:
        """Record and apply an operator decision (approve, modify or reject).
        An egress approval rewrites the bottle's policy (served on the next
        `/resolve`), then the Response unblocks the agent and an audit entry is
        written. Returns (ok, error)."""
        status = _RESPOND_STATUS.get(decision)
        if status is None:
            return False, f"unknown decision {decision!r}"
        try:
            proposal = self._supervisor.read_proposal(bottle_slug, proposal_id)
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

        self._supervisor.write_response(bottle_slug, Response(
            proposal_id=proposal_id, status=status, notes=notes, final_file=final_file,
        ))
        component = COMPONENT_FOR_TOOL.get(proposal.tool)
        if component is not None:
            self._supervisor.write_audit_entry(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                bottle_slug=bottle_slug,
                component=component,
                operator_action=status,
                operator_notes=notes,
                justification=proposal.justification,
                diff=render_diff(
                    diff_before, diff_after, label=component,
                    before_title="current", after_label="proposed",
                ),
            ))
        return True, ""

    # --- secret reprovision ---

    def reprovision_from_secret(self, bottle_id: str, env_var_secret: str) -> bool:
        """Restore a bottle's egress tokens by decrypting its stored rows with
        `env_var_secret`. False with no rows or on a decryption failure."""
        from .store.secret_store import decrypt_value
        encrypted = self.registry.get_agent_secrets(bottle_id)
        if not encrypted:
            return False
        try:
            decrypted = {
                k: decrypt_value(env_var_secret, v) for k, v in encrypted.items()
            }
        except ValueError:
            return False
        self._tokens[bottle_id] = decrypted
        return True

    def update_agent_secret(
        self, bottle_id: str, name: str, value: str, env_var_secret: str,
    ) -> bool:
        """Replace one egress token of a known bottle: in memory and as one row
        re-encrypted under `env_var_secret`. False if unknown. For refreshing a
        short-lived host credential without a relaunch."""
        from .store.secret_store import encrypt_value
        if self.registry.get(bottle_id) is None:
            return False
        self._tokens.setdefault(bottle_id, {})[name] = value
        self.registry.store_agent_secret(
            bottle_id, name, encrypt_value(env_var_secret, value))
        return True

    # --- gateway ---

    def gateway_status(self) -> dict[str, object]:
        """The gateway status for `GET /gateway`. The orchestrator doesn't run
        the gateway, so this reports `configured: false`."""
        return {"configured": False}


__all__ = ["OrchestratorCore"]
