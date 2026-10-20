"""FastAPI control-plane routes for the orchestrator."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import math
import sys
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..contract.auth import CONTROL_PLANE, ROLE_CLI, ROLES
from ..contract.supervise import TOOLS
from ..contract.http import (
    MAX_BODY_BYTES,
    ORCHESTRATOR_AUTH_HEADER,
    REQUEST_BODY_TIMEOUT_SECONDS,
)
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


class SecretBody(_StrictModel):
    name: StrictStr
    value: StrictStr
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
            scope.setdefault("state", {})["role"] = role
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
            body = await self._read_body(receive)
        except _BodyTooLarge:
            await self._reject(scope, send, 413, "request body too large")
            return
        except TimeoutError:
            await self._reject(scope, send, 408, "request body read timed out")
            return
        try:
            await self.app(scope, self._replay_body(body), send)
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
    async def _read_body(receive: Receive) -> bytes:
        body = bytearray()
        async with asyncio.timeout(REQUEST_BODY_TIMEOUT_SECONDS):
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY_BYTES:
                    raise _BodyTooLarge
                if not message.get("more_body", False):
                    break
        return bytes(body)

    @staticmethod
    def _replay_body(body: bytes) -> Receive:
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return replay


class _BodyTooLarge(Exception):
    """The streamed request exceeded the control-plane body limit."""


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

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: object, exc: RequestValidationError,
    ) -> JSONResponse:
        errors = exc.errors()
        location = errors[0].get("loc", ()) if errors else ()
        field = str(location[1]) if len(location) > 1 else ""
        suffix = f": {field}" if field else ""
        return JSONResponse(
            {"error": f"invalid request body{suffix}"},
            status_code=400,
        )

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

    @app.post("/bottles/{bottle_id}/secret")
    def update_secret(bottle_id: str, body: SecretBody) -> dict[str, object]:
        # Update ONE egress token for a running bottle in place — the
        # single-secret form of reprovision, for refreshing a short-lived host
        # credential (e.g. the Codex access token) without a relaunch. cli-only
        # (not in _GATEWAY_ROUTES): a bottle must never set its own tokens.
        if orch.update_agent_secret(
            bottle_id,
            _required(body.name, "name"),
            _required(body.value, "value"),
            _required(body.env_var_secret, "env_var_secret"),
        ):
            return {"updated": True}
        raise HTTPException(404, "no such bottle")

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
