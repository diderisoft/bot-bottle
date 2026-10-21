"""FastAPI control-plane routes for the orchestrator."""
# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import sys
from collections.abc import AsyncGenerator
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..contract.auth import CONTROL_PLANE, ROLE_CLI, ROLES
from ..contract.hostctl import BOTTLES_PATH, JOBS_PATH, ROLE_ORCHESTRATOR, LaunchRequest
from ..contract.supervise import TOOLS
from ..contract.http import (
    MAX_BODY_BYTES,
    ORCHESTRATOR_AUTH_HEADER,
    REQUEST_BODY_TIMEOUT_SECONDS,
)
from ..lib import log
from .hostctl_relay import (
    HostControlRefused, HostControlRelay, HostControlUnreachable, LiveBottlesUnknown,
)
from .relay_attach import pump_attach
from .service import LiveSource, OrchestratorCore

# How often the orchestrator reconciles its registry against the host
# controller's live bottles when a host controller brought it up.
RECONCILE_INTERVAL_SECONDS = 60.0
# The timer's grace window. Docker and firecracker register a bottle before
# its container or VM exists, so a scheduled round must leave a slow launch
# well alone; nothing waits on the timer, so being late costs little.
RECONCILE_TIMER_GRACE_SECONDS = 600.0

_GATEWAY_ROUTES = frozenset({
    ("POST", "/resolve"),
    ("POST", "/supervise/propose"),
    ("POST", "/supervise/poll"),
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class RegisterBody(_StrictModel):
    source_ip: StrictStr
    metadata: StrictStr = ""
    policy: StrictStr = ""
    tokens: dict[StrictStr, StrictStr] = {}
    env_var_secret: StrictStr = ""


class LaunchesBody(_StrictModel):
    """`POST /launches`: what a remote caller may ask the host controller for.
    Unknown fields (a path, secret values) are dropped, never forwarded."""

    agent: StrictStr
    bottles: list[StrictStr] = []
    label: StrictStr = ""
    color: StrictStr = ""
    prompt: StrictStr = ""
    headless: StrictBool = False
    image_policy: StrictStr = "fresh"
    assume_yes: StrictBool = False


class PolicyBody(_StrictModel):
    policy: StrictStr


class ReprovisionBody(_StrictModel):
    env_var_secret: StrictStr


class SecretBody(_StrictModel):
    name: StrictStr
    value: StrictStr
    env_var_secret: StrictStr


class ReconcileBody(_StrictModel):
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

    def _role(self, scope: Scope) -> str | None:
        presented = dict(scope["headers"]).get(
            ORCHESTRATOR_AUTH_HEADER.encode(), b"",
        ).decode(errors="ignore")
        return CONTROL_PLANE.verify(presented, self.signing_key)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Every WebSocket route is the cli role's; refuse before accepting."""
        role = self._role(scope)
        if role != ROLE_CLI:
            await send({"type": "websocket.close", "code": 1008})
            return
        scope.setdefault("state", {})["role"] = role
        await self.app(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await self._websocket(scope, receive, send)
            else:
                await self.app(scope, receive, send)
            return
        method = scope["method"]
        route = scope["path"].rstrip("/") or "/"
        if not (method == "GET" and route == "/health"):
            role = self._role(scope)
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
            await self.app(scope, self._replay_body(body, receive), send)
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
    def _replay_body(body: bytes, receive: Receive) -> Receive:
        """The body already read, then the client's own messages, so a
        streamed response still learns when the client goes away."""
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return replay


class _BodyTooLarge(Exception):
    """The streamed request exceeded the control-plane body limit."""


def _required(value: str, name: str) -> str:
    if not value:
        raise HTTPException(400, f"{name} (string) is required")
    return value


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _id(value: str, name: str) -> str:
    if not _ID.match(value):
        raise HTTPException(400, f"invalid {name}")
    return value


async def reconcile_periodically(
    orch: OrchestratorCore, hostctl: LiveSource, interval: float,
) -> None:
    """Reconcile every `interval` seconds until cancelled, so orphans are
    reaped without a CLI running. A failed round is logged and the next
    one tries again."""
    while True:
        await asyncio.sleep(interval)
        try:
            reaped = await asyncio.to_thread(
                orch.reconcile, hostctl, grace_seconds=RECONCILE_TIMER_GRACE_SECONDS)
        except LiveBottlesUnknown:
            continue  # reconcile logged why
        except Exception as exc:  # noqa: BLE001 - the timer must outlive one bad round
            log.warn(f"registry reconciliation failed [error_type={type(exc).__name__}]")
            continue
        if reaped:
            log.info(f"reaped {len(reaped)} orphaned bottle(s): {', '.join(reaped)}")


def create_app(
    orch: OrchestratorCore, *, signing_key: str, hostctl: HostControlRelay | None = None,
    reconcile_interval: float = RECONCILE_INTERVAL_SECONDS,
) -> FastAPI:
    """Build the authenticated orchestrator ASGI application. `hostctl` is
    the host controller remote launches go to and reconcile asks which
    bottles are live; None when this orchestrator wasn't brought up by one.
    With it, the app reconciles every `reconcile_interval` seconds while it
    runs."""
    key = signing_key.strip()
    if not key:
        raise ValueError(
            "orchestrator control-plane signing key is required; "
            "refusing to start without caller authentication"
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        timer = (asyncio.create_task(reconcile_periodically(orch, hostctl, reconcile_interval))
                 if hostctl is not None else None)
        try:
            yield
        finally:
            if timer is not None:
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer

    app = FastAPI(
        title="bot-bottle orchestrator",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
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
    def register(body: RegisterBody) -> dict[str, str]:
        rec = orch.register_bottle(
            _required(body.source_ip, "source_ip"),
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
    def deregister(bottle_id: str) -> dict[str, object]:
        if orch.deregister_bottle(bottle_id):
            return {"torn_down": True}
        raise HTTPException(404, "no such bottle")

    @app.post("/reconcile")
    def reconcile(body: ReconcileBody) -> dict[str, object]:
        kwargs: dict[str, float] = {}
        if body.grace_seconds is not None:
            if not math.isfinite(body.grace_seconds) or body.grace_seconds < 0:
                raise HTTPException(
                    400, "grace_seconds must be a non-negative finite number",
                )
            kwargs["grace_seconds"] = body.grace_seconds
        try:
            return {"reaped": orch.reconcile(hostctl, **kwargs)}
        except LiveBottlesUnknown as e:
            raise HTTPException(503, f"reconciliation skipped: {e}") from None

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

    # --- remote launch and attach, through the host controller (PRD 0092) ----

    def relay() -> HostControlRelay:
        if hostctl is None:
            raise HTTPException(503, "host controller not available")
        return hostctl

    def relayed(method: str, path: str, body: dict[str, object] | None = None) -> JSONResponse:
        try:
            status, reply = relay().request(method, path, body)
        except HostControlUnreachable as e:
            raise HTTPException(502, str(e)) from None
        return JSONResponse(reply, status_code=status)

    @app.post("/launches")
    def launch(body: LaunchesBody) -> JSONResponse:
        fields: dict[str, object] = body.model_dump()
        try:
            # The host controller's own rules, checked here first.
            LaunchRequest.from_json(fields, ROLE_ORCHESTRATOR)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e)) from None
        return relayed("POST", BOTTLES_PATH, fields)

    @app.get("/launches/{job_id}")
    def launch_status(job_id: str) -> JSONResponse:
        return relayed("GET", f"{JOBS_PATH}/{_id(job_id, 'job id')}")

    @app.get("/launches/{job_id}/events")
    def launch_events(job_id: str) -> StreamingResponse:
        path = f"{JOBS_PATH}/{_id(job_id, 'job id')}/events"
        try:
            lines = relay().stream(path)
        except HostControlRefused as e:
            raise HTTPException(e.status, str(e.body.get("error", e))) from None
        except HostControlUnreachable as e:
            raise HTTPException(502, str(e)) from None
        return StreamingResponse(lines, media_type="application/x-ndjson")

    @app.websocket("/bottles/{bottle_id}/attach")
    async def attach(websocket: WebSocket, bottle_id: str, cols: int = 80,
                     rows: int = 24) -> None:
        # A registry bottle id names its session by the slug it registered.
        session = orch.session_for(bottle_id)
        if hostctl is None or not _ID.match(session):
            await websocket.close(code=1011, reason="host controller not available"
                                  if hostctl is None else "invalid bottle id")
            return
        await pump_attach(websocket, hostctl, session, cols=cols, rows=rows)

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
    "RECONCILE_INTERVAL_SECONDS",
    "RECONCILE_TIMER_GRACE_SECONDS",
    "create_app",
    "reconcile_periodically",
]
