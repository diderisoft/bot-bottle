"""FastAPI control-plane routes for the orchestrator."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import math
import sys
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..orchestrator_auth import ROLE_CLI, ROLES
from ..supervisor.types import TOOLS
from ..trust_domain import CONTROL_PLANE
from .http_contract import MAX_BODY_BYTES, ORCHESTRATOR_AUTH_HEADER
from .service import OrchestratorCore

_GATEWAY_ROUTES = frozenset({
    ("POST", "/resolve"),
    ("POST", "/supervise/propose"),
    ("POST", "/supervise/poll"),
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class LaunchBody(_StrictModel):
    source_ip: StrictStr
    image_ref: StrictStr = ""
    metadata: StrictStr = ""
    policy: StrictStr = ""
    tokens: dict[StrictStr, StrictStr] = {}
    env_var_secret: StrictStr = ""


class PolicyBody(_StrictModel):
    policy: StrictStr


class ReprovisionBody(_StrictModel):
    env_var_secret: StrictStr


class ReconcileBody(_StrictModel):
    live_source_ips: list[StrictStr]
    grace_seconds: float | None = None


class IdentityBody(_StrictModel):
    source_ip: StrictStr
    identity_token: StrictStr = ""


class AttributeBody(_StrictModel):
    source_ip: StrictStr
    identity_token: StrictStr


class RespondBody(_StrictModel):
    proposal_id: StrictStr
    bottle_slug: StrictStr
    decision: StrictStr
    notes: StrictStr = ""
    final_file: StrictStr | None = None


class ProposeBody(IdentityBody):
    tool: StrictStr
    proposed_file: StrictStr
    justification: StrictStr


class PollBody(IdentityBody):
    proposal_id: StrictStr


class ControlPlaneBoundary:
    """Reject unauthenticated and oversized requests before reading a body."""

    def __init__(self, app: ASGIApp, signing_key: str) -> None:
        self.app = app
        self.signing_key = signing_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        route = scope["path"].rstrip("/") or "/"
        if not (method == "GET" and route == "/health"):
            headers = dict(scope["headers"])
            presented = headers.get(
                ORCHESTRATOR_AUTH_HEADER.encode(), b"",
            ).decode(errors="ignore")
            role = CONTROL_PLANE.verify(presented, self.signing_key)
            if role is None:
                await self._reject(
                    scope, send, 401, "control-plane authentication required",
                )
                return
            allowed = ROLES if (method, route) in _GATEWAY_ROUTES else {ROLE_CLI}
            if role not in allowed:
                await self._reject(scope, send, 403, "insufficient role for this route")
                return
            scope["state"]["role"] = role
        raw_length = dict(scope["headers"]).get(b"content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                await self._reject(scope, send, 400, "invalid Content-Length")
                return
            if length < 0:
                await self._reject(scope, send, 400, "invalid Content-Length")
                return
            if length > MAX_BODY_BYTES:
                await self._reject(scope, send, 413, "request body too large")
                return
        try:
            await self.app(scope, self._bounded_receive(receive), send)
        except Exception as exc:  # noqa: BLE001 - redact control-plane failures
            sys.stderr.write(
                f"orchestrator: {method} {route} failed "
                f"[error_type={type(exc).__name__}]\n"
            )
            sys.stderr.flush()
            await self._reject(scope, send, 500, "internal error")

    @staticmethod
    async def _reject(
        scope: Scope, send: Send, status: int, error: str,
    ) -> None:
        response = JSONResponse({"error": error}, status_code=status)
        await response(scope, ControlPlaneBoundary._empty_receive, send)

    @staticmethod
    async def _empty_receive() -> Message:
        return {"type": "http.disconnect"}

    @staticmethod
    def _bounded_receive(receive: Receive) -> Receive:
        consumed = 0

        async def bounded() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_BODY_BYTES:
                    raise HTTPException(413, "request body too large")
            return message

        return bounded


def _required(value: str, name: str) -> str:
    if not value:
        raise HTTPException(400, f"{name} (string) is required")
    return value


def create_app(orch: OrchestratorCore, *, signing_key: str) -> FastAPI:
    """Build the authenticated orchestrator ASGI application."""
    key = signing_key.strip()
    if not key:
        raise ValueError(
            "orchestrator control-plane signing key is required; "
            "refusing to start without caller authentication"
        )
    app = FastAPI(
        title="bot-bottle orchestrator",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(ControlPlaneBoundary, signing_key=key)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/gateway")
    def gateway() -> dict[str, object]:
        return orch.gateway_status()

    @app.get("/bottles")
    def bottles() -> dict[str, object]:
        return {"bottles": [record.redacted() for record in orch.registry.all()]}

    @app.post("/bottles", status_code=201)
    def launch(body: LaunchBody) -> dict[str, str]:
        rec = orch.launch_bottle(
            _required(body.source_ip, "source_ip"),
            image_ref=body.image_ref,
            metadata=body.metadata,
            policy=body.policy,
            tokens=dict(body.tokens),
            env_var_secret=body.env_var_secret,
        )
        return {"bottle_id": rec.bottle_id, "identity_token": rec.identity_token}

    @app.put("/bottles/{bottle_id}/policy")
    def set_policy(bottle_id: str, body: PolicyBody) -> dict[str, object]:
        if orch.set_policy(bottle_id, body.policy):
            return {"updated": True}
        raise HTTPException(404, "no such bottle")

    @app.post("/bottles/{bottle_id}/reprovision_gateway")
    def reprovision(bottle_id: str, body: ReprovisionBody) -> dict[str, object]:
        secret = _required(body.env_var_secret, "env_var_secret")
        if orch.reprovision_from_secret(bottle_id, secret):
            return {"reprovisioned": True}
        raise HTTPException(404, "no stored secrets for this bottle")

    @app.delete("/bottles/{bottle_id}")
    def teardown(bottle_id: str) -> dict[str, object]:
        if orch.teardown_bottle(bottle_id):
            return {"torn_down": True}
        raise HTTPException(404, "no such bottle")

    @app.post("/reconcile")
    def reconcile(body: ReconcileBody) -> dict[str, object]:
        if any(not ip for ip in body.live_source_ips):
            raise HTTPException(400, "live_source_ips must contain non-empty strings")
        kwargs: dict[str, float] = {}
        if body.grace_seconds is not None:
            if not math.isfinite(body.grace_seconds) or body.grace_seconds < 0:
                raise HTTPException(
                    400, "grace_seconds must be a non-negative finite number",
                )
            kwargs["grace_seconds"] = body.grace_seconds
        return {"reaped": orch.reconcile(body.live_source_ips, **kwargs)}

    @app.post("/attribute")
    def attribute(body: AttributeBody) -> dict[str, str]:
        rec = orch.attribute(body.source_ip, body.identity_token)
        if rec is None:
            raise HTTPException(403, "unattributed")
        return {"bottle_id": rec.bottle_id}

    @app.get("/supervise/proposals")
    def proposals() -> dict[str, object]:
        return {"proposals": orch.supervise_pending()}

    @app.post("/supervise/respond")
    def respond(body: RespondBody) -> dict[str, object]:
        ok, error = orch.supervise_respond(
            _required(body.proposal_id, "proposal_id"),
            bottle_slug=_required(body.bottle_slug, "bottle_slug"),
            decision=_required(body.decision, "decision"),
            notes=body.notes,
            final_file=body.final_file,
        )
        if not ok:
            raise HTTPException(409, error)
        return {"responded": True}

    @app.post("/supervise/propose", status_code=201)
    def propose(body: ProposeBody) -> dict[str, str]:
        source_ip = _required(body.source_ip, "source_ip")
        if body.tool not in TOOLS:
            raise HTTPException(400, f"tool (string) must be one of {TOOLS}")
        rec = orch.resolve(source_ip, body.identity_token)
        if rec is None:
            raise HTTPException(403, "unattributed")
        proposal_id = orch.supervise_queue_proposal(
            rec.bottle_id,
            tool=body.tool,
            proposed_file=_required(body.proposed_file, "proposed_file"),
            justification=_required(body.justification, "justification"),
        )
        return {"proposal_id": proposal_id}

    @app.post("/supervise/poll")
    def poll(body: PollBody) -> dict[str, object]:
        rec = orch.resolve(
            _required(body.source_ip, "source_ip"), body.identity_token,
        )
        if rec is None:
            raise HTTPException(403, "unattributed")
        return orch.supervise_poll_response(
            rec.bottle_id, _required(body.proposal_id, "proposal_id"),
        )

    @app.post("/resolve")
    def resolve(body: IdentityBody) -> dict[str, object]:
        rec = orch.resolve(
            _required(body.source_ip, "source_ip"), body.identity_token,
        )
        if rec is None:
            raise HTTPException(403, "unattributed")
        return {
            "bottle_id": rec.bottle_id,
            "policy": rec.policy,
            "tokens": orch.tokens_for(rec.bottle_id),
        }

    return app


__all__ = [
    "ControlPlaneBoundary",
    "MAX_BODY_BYTES",
    "ORCHESTRATOR_AUTH_HEADER",
    "create_app",
]
