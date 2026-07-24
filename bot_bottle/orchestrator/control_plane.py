"""Orchestrator HTTP control plane (PRD 0070).

The backend-agnostic control-plane RPC (CLI / console -> orchestrator) over
**HTTP** — the universal transport chosen in 0070 (works on every host; no
vsock / unix-socket portability caveats):

    GET    /health                    -> 200 {"status": "ok"}
    GET    /gateway                   -> 200 {"configured", ["name","running"]}
    GET    /bottles                   -> 200 {"bottles": [ <redacted record>, ...]}
    POST   /bottles                   -> 201 {"bottle_id","identity_token"}  (launch)
                                         body: {"source_ip", ["image_ref"],
                                                ["metadata"], ["policy"],
                                                ["tokens"], ["env_var_secret"]}
    PUT    /bottles/<bottle_id>/policy -> 200 {"updated": true} | 404  (live reload)
                                         body: {"policy"}
    POST   /bottles/<bottle_id>/reprovision_gateway
                                      -> 200 {"reprovisioned": true} | 404
                                         body: {"env_var_secret"}
    DELETE /bottles/<bottle_id>        -> 200 {"torn_down": true} | 404  (teardown)
    POST   /reconcile                 -> 200 {"reaped": [bottle_id, ...]}
                                         body: {"live_source_ips": [...],
                                                ["grace_seconds"]}
    POST   /attribute                 -> 200 {"bottle_id"} | 403
    POST   /resolve                   -> 200 {"bottle_id","policy"} | 403
                                         body: {"source_ip","identity_token"}
    GET    /supervise/proposals       -> 200 {"proposals": [ <proposal>, ...]}
    POST   /supervise/respond         -> 200 {"responded": true} | 409  (operator)
                                         body: {"proposal_id","bottle_slug",
                                                "decision", ["notes"],["final_file"]}
    POST   /supervise/propose         -> 201 {"proposal_id"} | 403  (agent)
                                         body: {"source_ip","identity_token",
                                                "tool","proposed_file","justification"}
    POST   /supervise/poll            -> 200 {"status", ["notes"],["final_file"]} | 403
                                         body: {"source_ip","identity_token",
                                                "proposal_id"}

The `/supervise/propose` + `/supervise/poll` pair is the **agent** half of the
supervise flow: the data plane (supervise / egress / git-gate) queues a
proposal and polls for its response over RPC instead of opening `bot-bottle.db`
directly. Both attribute the caller by `(source_ip, identity_token)` exactly
like `/resolve`, so a bottle can only ever queue or read its own proposals.

`POST /bottles` / `DELETE` drive the full launch lifecycle: they mint (or
tear down) the bottle in the registry AND broker the backend-native launch
via the orchestrator. Register/deregister without a launch are internal to
`Orchestrator`, not exposed here.

Routing/handling is the pure function `dispatch()` so it is unit-testable
without a socket; `Handler` / `ControlPlaneServer` / `make_server` are a
thin stdlib adapter around it. Listing redacts identity tokens — they are
returned only once, to the caller that launches the bottle.
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import socketserver
import sys
import typing
from urllib.parse import urlsplit

from ..paths import CONTROL_PLANE_TOKEN_ENV
from ..supervise_types import TOOLS
from .service import Orchestrator

# JSON body payload type (parsed request / rendered response).
Json = dict[str, object]

# The request header carrying the per-host control-plane secret. Every route
# except `GET /health` requires it (see `dispatch`). The trusted callers hold
# the secret (the gateway's PolicyResolver, the host CLI's OrchestratorClient);
# an agent that can merely *reach* the port cannot present it, so it can't
# enumerate bottles, rewrite policy, read injected upstream tokens, or approve
# its own supervise proposals.
CONTROL_AUTH_HEADER = "x-bot-bottle-control-auth"


def _parse_json_object(body: bytes) -> Json:
    """Parse a JSON object body. Raises ValueError for non-objects / bad JSON."""
    if not body:
        return {}
    obj = json.loads(body)  # raises json.JSONDecodeError (a ValueError)
    if not isinstance(obj, dict):
        raise ValueError("request body must be a JSON object")
    return obj


def dispatch(  # pylint: disable=too-many-return-statements,too-many-branches
    orch: Orchestrator, method: str, path: str, body: bytes, *, authorized: bool = True,
) -> tuple[int, Json]:
    """Route one control-plane request to a (status, payload) pair. Pure —
    no I/O beyond the orchestrator — so it is fully testable without a socket.

    `authorized` is whether the request presented the control-plane secret (or
    no secret is configured — see `ControlPlaneServer`). Every route except
    `GET /health` requires it: the source-IP + identity-token checks inside
    `/resolve` and `/attribute` authenticate the *bottle* a request is about,
    not the *caller*, so without this gate any agent that can reach the port
    could rewrite another bottle's policy, read the injected upstream tokens,
    or approve its own supervise proposals. Defaults True so unit tests of the
    routing logic don't have to thread it through."""
    route = urlsplit(path).path.rstrip("/") or "/"

    if method == "GET" and route == "/health":
        return 200, {"status": "ok"}

    if not authorized:
        # Everything below is a trusted-caller operation. Deny before touching
        # the registry / broker / supervise store.
        return 401, {"error": "control-plane authentication required"}

    if method == "GET" and route == "/gateway":
        return 200, orch.gateway_status()

    if method == "GET" and route == "/bottles":
        return 200, {"bottles": [r.redacted() for r in orch.registry.all()]}

    if method == "POST" and route == "/bottles":
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        image_ref = data.get("image_ref")
        metadata = data.get("metadata")
        policy = data.get("policy")
        raw_tokens = data.get("tokens")
        tokens = {
            k: v for k, v in raw_tokens.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw_tokens, dict) else {}
        env_var_secret = data.get("env_var_secret", "")
        rec = orch.launch_bottle(
            source_ip,
            image_ref=image_ref if isinstance(image_ref, str) else "",
            metadata=metadata if isinstance(metadata, str) else "",
            policy=policy if isinstance(policy, str) else "",
            tokens=tokens,
            env_var_secret=env_var_secret if isinstance(env_var_secret, str) else "",
        )
        return 201, {"bottle_id": rec.bottle_id, "identity_token": rec.identity_token}

    if method == "PUT" and route.startswith("/bottles/") and route.endswith("/policy"):
        bottle_id = route[len("/bottles/"):-len("/policy")]
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        policy = data.get("policy")
        if not isinstance(policy, str):
            return 400, {"error": "policy (string) is required"}
        if orch.set_policy(bottle_id, policy):
            return 200, {"updated": True}
        return 404, {"error": "no such bottle"}

    if (
        method == "POST"
        and route.startswith("/bottles/")
        and route.endswith("/reprovision_gateway")
    ):
        bottle_id = route[len("/bottles/") : -len("/reprovision_gateway")]
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        env_var_secret = data.get("env_var_secret")
        if not isinstance(env_var_secret, str) or not env_var_secret:
            return 400, {"error": "env_var_secret (string) is required"}
        if orch.reprovision_from_secret(bottle_id, env_var_secret):
            return 200, {"reprovisioned": True}
        return 404, {"error": "no stored secrets for this bottle"}

    if method == "DELETE" and route.startswith("/bottles/"):
        bottle_id = route[len("/bottles/"):]
        if orch.teardown_bottle(bottle_id):
            return 200, {"torn_down": True}
        return 404, {"error": "no such bottle"}

    if method == "POST" and route == "/reconcile":
        # Host-driven self-heal: the caller enumerates its live bottles (only
        # the host can see the backend) and the orchestrator drops rows for
        # every other active bottle. Trusted-caller only — an agent that could
        # reach this would be able to unregister its neighbours.
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        raw_ips = data.get("live_source_ips")
        if not isinstance(raw_ips, list):
            return 400, {"error": "live_source_ips (list of strings) is required"}
        live = [ip for ip in raw_ips if isinstance(ip, str) and ip]
        grace = data.get("grace_seconds")
        kwargs = (
            {"grace_seconds": float(grace)}
            if isinstance(grace, (int, float)) and not isinstance(grace, bool)
            else {}
        )
        return 200, {"reaped": orch.reconcile(live, **kwargs)}

    if method == "POST" and route == "/attribute":
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        token = data.get("identity_token")
        if not isinstance(source_ip, str) or not isinstance(token, str):
            return 400, {"error": "source_ip and identity_token (strings) required"}
        rec = orch.attribute(source_ip, token)
        if rec is None:
            return 403, {"error": "unattributed"}
        return 200, {"bottle_id": rec.bottle_id}

    if method == "GET" and route == "/supervise/proposals":
        # Operator TUI: pending supervise proposals across all bottles.
        return 200, {"proposals": orch.supervise_pending()}

    if method == "POST" and route == "/supervise/respond":
        # Operator decision: apply (approve/modify rewrites egress policy),
        # write the queued response, audit — all server-side on the one DB.
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        proposal_id = data.get("proposal_id")
        bottle_slug = data.get("bottle_slug")
        decision = data.get("decision")
        if not (isinstance(proposal_id, str) and proposal_id):
            return 400, {"error": "proposal_id (string) is required"}
        if not (isinstance(bottle_slug, str) and bottle_slug):
            return 400, {"error": "bottle_slug (string) is required"}
        if not (isinstance(decision, str) and decision):
            return 400, {"error": "decision (string) is required"}
        notes = data.get("notes")
        final_file = data.get("final_file")
        ok, err = orch.supervise_respond(
            proposal_id,
            bottle_slug=bottle_slug,
            decision=decision,
            notes=notes if isinstance(notes, str) else "",
            final_file=final_file if isinstance(final_file, str) else None,
        )
        if ok:
            return 200, {"responded": True}
        return 409, {"error": err}

    if method == "POST" and route == "/supervise/propose":
        # Agent half: queue a proposal, attributed to the caller resolved from
        # (source_ip, identity_token) — never a caller-supplied slug — so the
        # data plane can't forge attribution. Fail-closed 403 when unattributed.
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        token = data.get("identity_token")
        tool = data.get("tool")
        proposed_file = data.get("proposed_file")
        justification = data.get("justification")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        if not isinstance(tool, str) or tool not in TOOLS:
            return 400, {"error": f"tool (string) must be one of {TOOLS}"}
        if not isinstance(proposed_file, str) or not proposed_file:
            return 400, {"error": "proposed_file (string) is required"}
        if not isinstance(justification, str) or not justification:
            return 400, {"error": "justification (string) is required"}
        rec = orch.resolve(source_ip, token if isinstance(token, str) else "")
        if rec is None:
            return 403, {"error": "unattributed"}
        proposal_id = orch.supervise_queue_proposal(
            rec.bottle_id, tool=tool, proposed_file=proposed_file,
            justification=justification,
        )
        return 201, {"proposal_id": proposal_id}

    if method == "POST" and route == "/supervise/poll":
        # Agent half: non-blocking read of the caller's own proposal decision.
        # Attributed like /propose, and scoped to the resolved bottle id, so a
        # guessed proposal_id can never read another bottle's response.
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        token = data.get("identity_token")
        proposal_id = data.get("proposal_id")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        if not isinstance(proposal_id, str) or not proposal_id:
            return 400, {"error": "proposal_id (string) is required"}
        rec = orch.resolve(source_ip, token if isinstance(token, str) else "")
        if rec is None:
            return 403, {"error": "unattributed"}
        return 200, orch.supervise_poll_response(rec.bottle_id, proposal_id)

    if method == "POST" and route == "/resolve":
        # The per-request lookup the multi-tenant gateway makes: returns the
        # bottle's policy. Requires a matching (source_ip, identity_token)
        # pair — a missing/empty/mismatched token fail-closes (403), no
        # source-IP-only fallback.
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        token = data.get("identity_token")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        rec = orch.resolve(source_ip, token if isinstance(token, str) else "")
        if rec is None:
            return 403, {"error": "unattributed"}
        # tokens are the in-memory per-bottle egress auth values the gateway
        # injects; served here, never persisted.
        return 200, {
            "bottle_id": rec.bottle_id,
            "policy": rec.policy,
            "tokens": orch.tokens_for(rec.bottle_id),
        }

    return 404, {"error": "not found"}


class Handler(http.server.BaseHTTPRequestHandler):
    """Thin stdlib adapter: read the body, call `dispatch`, write JSON."""

    # Quiet by default (the orchestrator has its own logging); opt back into
    # stdlib access logging with BOT_BOTTLE_ORCHESTRATOR_DEBUG.
    def log_message(self, format: str, *args: typing.Any) -> None:  # noqa: A002
        if os.environ.get("BOT_BOTTLE_ORCHESTRATOR_DEBUG"):
            super().log_message(format, *args)

    def _serve(self, method: str) -> None:
        """Read the request body, dispatch it, and write the JSON reply. A
        dispatch failure (e.g. a broker error) returns a 500 rather than
        crashing the connection, so one bad request can't take the control
        plane down for the caller."""
        server = self.server
        assert isinstance(server, ControlPlaneServer)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        authorized = server.is_authorized(self.headers.get(CONTROL_AUTH_HEADER, ""))
        try:
            status, payload = dispatch(
                server.orchestrator, method, self.path, body, authorized=authorized)
        except Exception as e:  # noqa: BLE001 — the control plane must stay up
            sys.stderr.write(f"orchestrator: {method} {self.path} failed: {e!r}\n")
            sys.stderr.flush()
            status, payload = 500, {"error": f"internal error: {e}"}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_DELETE(self) -> None:
        self._serve("DELETE")


class ControlPlaneServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server that carries the orchestrator for its handlers.

    Holds the per-host control-plane secret (from `$BOT_BOTTLE_CONTROL_PLANE_TOKEN`,
    injected by the launcher into this container only). When a secret is set,
    every route but `/health` requires it; when it is unset the server runs
    **open** and says so loudly at startup — a fail-visible fallback for tests
    and any backend that hasn't wired the secret yet (e.g. Firecracker, whose
    nft boundary already blocks agents from the control-plane port)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator
        self._auth_token = os.environ.get(CONTROL_PLANE_TOKEN_ENV, "").strip()
        if not self._auth_token:
            sys.stderr.write(
                "orchestrator: WARNING — no control-plane secret "
                f"(${CONTROL_PLANE_TOKEN_ENV}); running WITHOUT caller "
                "authentication. Any client that can reach this port can drive "
                "it. Backends that put the control plane on an agent-reachable "
                "network MUST set this.\n"
            )
            sys.stderr.flush()
        super().__init__(address, Handler)

    def is_authorized(self, presented: str) -> bool:
        """True iff the request may proceed past `/health`: either no secret is
        configured (open mode) or the presented header matches it. Constant-time
        compare so a wrong token leaks nothing timing-wise."""
        if not self._auth_token:
            return True
        return hmac.compare_digest(presented, self._auth_token)


def make_server(
    orchestrator: Orchestrator, host: str = "127.0.0.1", port: int = 0
) -> ControlPlaneServer:
    """Build (but do not start) a control-plane server. `port=0` binds an
    ephemeral port — read `server.server_address` for the actual one."""
    return ControlPlaneServer((host, port), orchestrator)


__all__ = [
    "dispatch", "Handler", "ControlPlaneServer", "make_server", "Json",
    "CONTROL_AUTH_HEADER",
]
