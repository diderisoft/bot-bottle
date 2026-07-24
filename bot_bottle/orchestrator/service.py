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
  * `reconcile` sweeps rows whose bottle is no longer running — the
    self-heal for the teardown paths that never got to run (a hard-killed
    launcher), since an orphan row at a recycled source IP bricks the next
    bottle that lands on it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone

from .broker import LaunchBroker, LaunchRequest, sign_request
from .registry import DEFAULT_REAP_GRACE_SECONDS, BottleRecord, RegistryStore
from .supervisor import (
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
    Supervisor,
    render_diff,
    sha256_hex,
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
        supervisor: Supervisor | None = None,
    ) -> None:
        self.registry = registry
        self._broker = broker
        self._secret = sign_secret
        # The supervise service (queue + audit I/O). Injectable so tests can
        # point it at a temp DB; defaults to the host DB.
        self._supervisor = supervisor or Supervisor()
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
        env_var_secret: str = "",
    ) -> BottleRecord:
        """Register a bottle (with its gateway policy + in-memory egress auth
        tokens) and broker its launch. Rolls the registry entry back if the
        launch doesn't take, so a failure leaves no orphan.

        When *env_var_secret* is provided alongside *tokens*, the token values
        are also encrypted and written to ``bottled_agent_secrets`` so they can
        survive an orchestrator restart (see ``reprovision_from_secret``)."""
        rec = self.registry.register(source_ip, metadata=metadata, policy=policy)
        if tokens:
            self._tokens[rec.bottle_id] = dict(tokens)
            if env_var_secret:
                from .store.secret_store import encrypt_value
                encrypted = {k: encrypt_value(env_var_secret, v) for k, v in tokens.items()}
                self.registry.store_agent_secrets(rec.bottle_id, encrypted)
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
        # Reap any supervise proposals the gone bottle never acknowledged.
        self._supervisor.archive_all_proposals(bottle_id)
        return True

    def reconcile(
        self,
        live_source_ips: Iterable[str],
        *,
        grace_seconds: float = DEFAULT_REAP_GRACE_SECONDS,
    ) -> list[str]:
        """Drop registry rows for bottles that are no longer running, and
        forget their in-memory egress tokens. Returns the reaped bottle ids.

        The caller supplies the live set because only the host can enumerate
        its own containers — the orchestrator runs *inside* the infra
        container and has no view of the backend. Deliberately does not
        broker a teardown: the container is already gone, so there is nothing
        to stop, and a broker error must not stop the sweep from clearing
        the row that would otherwise brick the next bottle at that address.

        See `RegistryStore.reap_absent` for why orphans accumulate and why
        they are harmful rather than merely untidy."""
        reaped = self.registry.reap_absent(
            live_source_ips, grace_seconds=grace_seconds)
        for rec in reaped:
            self._tokens.pop(rec.bottle_id, None)
            # Reap any supervise proposals the gone bottle never acknowledged.
            self._supervisor.archive_all_proposals(rec.bottle_id)
        return [rec.bottle_id for rec in reaped]

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
    #
    # The *agent* half of the queue (queue a proposal, poll for its response)
    # is served here too (PRD 0070): the data plane no longer opens the DB, so
    # supervise_server / egress / git-gate reach the queue over the control
    # plane. Both are keyed on the caller's orchestrator-resolved bottle id —
    # never a caller-supplied slug — so a bottle can only queue or read its own
    # proposals even if the data plane is compromised.

    def supervise_queue_proposal(
        self, bottle_id: str, *, tool: str, proposed_file: str, justification: str,
    ) -> str:
        """Queue an agent-side proposal under `bottle_id` and return its id.

        `bottle_id` is the caller resolved from `(source_ip, identity_token)` by
        the control plane, so the proposal is attributed to the calling bottle,
        not to anything the (possibly hostile) data plane asserts. The
        proposed-file self-hash is computed here so the agent never supplies it."""
        proposal = Proposal.new(
            bottle_slug=bottle_id,
            tool=tool,
            proposed_file=proposed_file,
            justification=justification,
            current_file_hash=sha256_hex(proposed_file),
        )
        self._supervisor.write_proposal(proposal)
        return proposal.id

    def supervise_poll_response(self, bottle_id: str, proposal_id: str) -> dict[str, object]:
        """Non-blocking, **idempotent** poll of one of `bottle_id`'s proposals.

        Returns `{"status": ...}`: a terminal decision (`approved`/`modified`/
        `rejected`, with `notes` + `final_file`) once the operator has responded,
        `pending` while it's still queued undecided, or `unknown` when no such
        queued proposal exists for this bottle (wrong id, or already archived).

        Poll does **not** archive — a decided proposal keeps returning the same
        decision so a client whose connection drops after the decision but before
        it consumes the response still gets it on retry (issue #469 review). A
        decided proposal is already excluded from the operator's pending list (a
        response row exists), so it doesn't linger there; the row itself is reaped
        when the bottle is torn down / reconciled.

        Reads are scoped to `bottle_id` (the queue key), so a caller can never
        read another bottle's proposal even with a guessed id."""
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
        """All pending proposals across bottles, FIFO, as JSON dicts
        (`Proposal.to_dict`, round-trippable via `Proposal.from_dict`).

        Each dict carries an extra `bottle_label`: the bottle's human slug
        resolved from the registry (the proposal itself is keyed by the
        orchestrator-assigned bottle_id, which is opaque to an operator). The
        CLI renders the label but still responds against `bottle_slug`."""
        out: list[dict[str, object]] = []
        for p in self._supervisor.list_all_pending_proposals():
            d = p.to_dict()
            d["bottle_label"] = self._label_for(p.bottle_slug)
            out.append(d)
        return out

    def _label_for(self, bottle_slug: str) -> str:
        """The human slug recorded in registry metadata for a proposal's
        bottle, or the bottle_slug unchanged when the bottle is gone or has no
        recorded slug — so the label is always non-empty."""
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
        """The live registry record for a proposal's bottle, or None (e.g. the
        bottle was torn down before the operator responded).

        In consolidated mode the supervise server attributes each proposal to
        the orchestrator-assigned bottle_id and stores that as the proposal's
        `bottle_slug` (see supervise_server `_attributed_config`), so the fast
        path is a direct bottle_id lookup. The metadata-slug scan is the
        fallback for legacy single-tenant proposals keyed by the human slug."""
        rec = self.registry.get(slug)
        if rec is not None:
            return rec
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
                diff=render_diff(diff_before, diff_after, label=component),
            ))
        return True, ""

    # --- secret reprovision -----------------------------------------------

    def reprovision_from_secret(self, bottle_id: str, env_var_secret: str) -> bool:
        """Re-inject a bottle's egress tokens from its ENV_VAR_SECRET.

        Reads the encrypted rows from ``bottled_agent_secrets``, decrypts each
        value with *env_var_secret*, and restores ``_tokens[bottle_id]``.
        Returns True on success, False when no stored secrets exist for this
        bottle or decryption fails (wrong key / corrupt data)."""
        from .store.secret_store import decrypt_value
        encrypted = self.registry.get_agent_secrets(bottle_id)
        if not encrypted:
            return False
        try:
            self._tokens[bottle_id] = {k: decrypt_value(env_var_secret, v)
                                       for k, v in encrypted.items()}
        except ValueError:
            return False
        return True

    # --- consolidated gateway ----------------------------------------------

    def gateway_status(self) -> dict[str, object]:
        """Report the shared gateway for the control plane / console.

        The orchestrator no longer owns a standalone gateway lifecycle — the
        consolidated flow runs the gateway data plane inside the per-host infra
        container/VM (see `backend/*/gateway`), so this reports `configured:
        false`. Retained for the documented `GET /gateway` control-plane
        contract."""
        return {"configured": False}


__all__ = ["Orchestrator"]
