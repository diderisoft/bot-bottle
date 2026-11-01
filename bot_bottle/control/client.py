"""Host-side orchestrator control-plane client, used to register, re-policy and
tear down bottles. Unlike the gateway's fail-closed `PolicyResolver`, a
non-success here is an error to surface. Stdlib only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..environment import active_name, is_live
from ..lib.log import debug
from ..contract.auth import CONTROL_PLANE, ROLE_CLI
from ..contract.hostctl import Settings
from ..contract.supervise import ALREADY_ANSWERED
from ..trust_domain import mint
from ..contract.http import ORCHESTRATOR_AUTH_HEADER
from ..paths import bot_bottle_root

DEFAULT_TIMEOUT_SECONDS = 5.0


def _host_auth_token() -> str:
    """A fresh `cli` token signed with the host control-plane key, or "" if the
    key can't be read ("" sends no auth header, which a secured control plane
    rejects)."""
    try:
        return mint(CONTROL_PLANE, ROLE_CLI)
    except (OSError, ValueError):
        return ""


class OrchestratorClientError(RuntimeError):
    """A control-plane call failed: unreachable (`status` None), or an
    unexpected `status`."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AlreadyAnswered(OrchestratorClientError):
    """A supervise proposal had a decision before ours; the first stands.
    `decided_as` is that decision's status ("approved", "rejected", ...)."""

    def __init__(self, decided_as: str) -> None:
        super().__init__(f"already answered ({decided_as})")
        self.decided_as = decided_as


@dataclass(frozen=True)
class RegisteredBottle:
    """`POST /bottles`'s result: the bottle id and the agent's identity token.
    The caller sets `env_var_secret` for the agent's env."""

    bottle_id: str
    identity_token: str
    env_var_secret: str = ""


@dataclass(frozen=True)
class BackendProbeFailure:
    """Safe diagnostic for an optional backend discovery probe."""

    backend: str
    error_type: str


def _probe_failure(backend: str, exc: BaseException) -> BackendProbeFailure:
    failure = BackendProbeFailure(backend, type(exc).__name__)
    debug(
        "orchestrator discovery probe unavailable",
        context={"backend": failure.backend, "error_type": failure.error_type},
    )
    return failure


class OrchestratorClient:
    """Trusted host-side client for the control plane. Sends a `cli` token on
    every call (all routes but `/health` need one); `auth_token` overrides it
    for tests."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        auth_token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._auth_token = auth_token if auth_token is not None else _host_auth_token()

    def _request(
        self, method: str, path: str, body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        """Send one request; return `(status, payload)`. Raises
        `OrchestratorClientError` only when unreachable or the data is
        malformed; statuses are returned so callers can treat 404 as "no such
        bottle"."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if self._auth_token:
            headers[ORCHESTRATOR_AUTH_HEADER] = self._auth_token
        req = urllib.request.Request(
            f"{self._base}{path}", data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                payload = json.loads(raw) if raw else {}
                return resp.status, payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as e:
            # A structured error still carries a status.
            try:
                payload = json.loads(e.read() or b"{}")
            except (ValueError, OSError):
                payload = {}
            return e.code, payload if isinstance(payload, dict) else {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise OrchestratorClientError(f"{method} {path}: {e}") from e

    def _ok(self, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        """`_request` requiring a 2xx."""
        status, payload = self._request(method, path, body)
        if not 200 <= status < 300:
            detail = payload.get("error", "")
            raise OrchestratorClientError(
                f"{method} {path}: HTTP {status} {detail}".rstrip(), status=status)
        return payload

    def health(self) -> bool:
        """True iff `GET /health` answers 200."""
        try:
            status, _ = self._request("GET", "/health")
        except OrchestratorClientError:
            return False
        return status == 200

    def version(self) -> dict[str, object] | None:
        """GET /version (public, no auth). None when unreachable or not found."""
        try:
            status, payload = self._request("GET", "/version")
        except OrchestratorClientError:
            return None
        return payload if status == 200 else None

    def register_bottle(
        self,
        source_ip: str,
        *,
        metadata: str = "",
        policy: str = "",
        tokens: dict[str, str] | None = None,
        env_var_secret: str = "",
    ) -> RegisteredBottle:
        """Register a bottle (`POST /bottles`). `tokens` are its egress auth
        values (env name -> value); with `env_var_secret` the orchestrator also
        stores them encrypted for restart recovery. Returns the id and identity
        token."""
        payload = self._ok("POST", "/bottles", {
            "source_ip": source_ip,
            "metadata": metadata,
            "policy": policy,
            "tokens": tokens or {},
            "env_var_secret": env_var_secret,
        })
        bottle_id = payload.get("bottle_id")
        token = payload.get("identity_token")
        if not isinstance(bottle_id, str) or not isinstance(token, str):
            raise OrchestratorClientError("register: response missing bottle_id/identity_token")
        return RegisteredBottle(bottle_id=bottle_id, identity_token=token)

    def reprovision_gateway(self, bottle_id: str, env_var_secret: str) -> bool:
        """Restore a bottle's egress tokens from its ENV_VAR_SECRET (`POST
        /bottles/<id>/reprovision_gateway`). False when nothing is stored for
        it."""
        status, _ = self._request(
            "POST",
            f"/bottles/{bottle_id}/reprovision_gateway",
            {"env_var_secret": env_var_secret},
        )
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(
                f"reprovision_gateway {bottle_id}: HTTP {status}", status=status)
        return True

    def update_agent_secret(
        self, bottle_id: str, name: str, value: str, env_var_secret: str,
    ) -> bool:
        """Replace one egress token of a running bottle (`POST
        /bottles/<id>/secret`), e.g. a refreshed host credential. False for an
        unknown bottle."""
        status, _ = self._request(
            "POST",
            f"/bottles/{bottle_id}/secret",
            {"name": name, "value": value, "env_var_secret": env_var_secret},
        )
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(
                f"update_agent_secret {bottle_id}: HTTP {status}", status=status)
        return True

    def teardown_bottle(self, bottle_id: str) -> bool:
        """`DELETE /bottles/<id>`. False if unknown; idempotent."""
        status, _ = self._request("DELETE", f"/bottles/{bottle_id}")
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(f"teardown {bottle_id}: HTTP {status}", status=status)
        return True

    def reconcile(self, *, grace_seconds: float | None = None) -> list[str]:
        """Drop rows for bottles no longer running (`POST /reconcile`); returns
        the reaped ids. Raises on the 503 sent when live bottles can't be
        determined."""
        body: dict[str, object] = {}
        if grace_seconds is not None:
            body["grace_seconds"] = grace_seconds
        payload = self._ok("POST", "/reconcile", body)
        reaped = payload.get("reaped")
        return [r for r in reaped if isinstance(r, str)] if isinstance(reaped, list) else []

    def set_policy(self, bottle_id: str, policy: str) -> bool:
        """Replace a bottle's policy (`PUT /bottles/<id>/policy`). False if
        unknown."""
        status, _ = self._request("PUT", f"/bottles/{bottle_id}/policy", {"policy": policy})
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise OrchestratorClientError(f"set_policy {bottle_id}: HTTP {status}",
                                          status=status)
        return True

    def list_bottles(self) -> list[dict[str, object]]:
        """Every bottle's redacted record (`GET /bottles`)."""
        payload = self._ok("GET", "/bottles")
        bottles = payload.get("bottles")
        return bottles if isinstance(bottles, list) else []

    # --- supervise queue ---

    def supervise_pending(self) -> list[dict[str, object]]:
        """Pending proposals across bottles (`GET /supervise/proposals`)."""
        payload = self._ok("GET", "/supervise/proposals")
        proposals = payload.get("proposals")
        return proposals if isinstance(proposals, list) else []

    def supervise_respond(
        self,
        proposal_id: str,
        *,
        bottle_slug: str,
        decision: str,
        notes: str = "",
        final_file: str | None = None,
    ) -> None:
        """Record a decision (`POST /supervise/respond`): approve, modify or
        reject. Raises `AlreadyAnswered` if it already had a decision (the
        first stands), and `OrchestratorClientError` if the proposal is gone
        or can't be applied."""
        body: dict[str, object] = {
            "proposal_id": proposal_id,
            "bottle_slug": bottle_slug,
            "decision": decision,
            "notes": notes,
        }
        if final_file is not None:
            body["final_file"] = final_file
        status, payload = self._request("POST", "/supervise/respond", body)
        detail = str(payload.get("detail") or payload.get("error") or "")
        if status == 409 and detail.startswith(ALREADY_ANSWERED):
            raise AlreadyAnswered(detail[len(ALREADY_ANSWERED):].strip(" ()") or "unknown")
        if not 200 <= status < 300:
            raise OrchestratorClientError(
                f"POST /supervise/respond: HTTP {status} {detail}".rstrip(), status=status)


# --- the environment's orchestrator ---
#
# An environment runs exactly one orchestrator. Its bring-up
# (`InfraService.ensure_running`) records which backend's it is and the URL the
# host reaches it at; everything that talks to "the orchestrator" uses that
# record, never another backend's orchestrator that happens to be listening.

ORCHESTRATOR_RECORD = "orchestrator.json"


@dataclass(frozen=True)
class OrchestratorEndpoint:
    """An orchestrator: the backend running it and its host control-plane URL."""

    backend: str
    url: str


def orchestrator_record_path(root: Path | None = None) -> Path:
    """Where the environment records its orchestrator: `<root>/infra/`."""
    return (root if root is not None else bot_bottle_root()) / "infra" / ORCHESTRATOR_RECORD


def recorded_orchestrator(root: Path | None = None) -> OrchestratorEndpoint | None:
    """The orchestrator the environment's last bring-up recorded, or None
    (never brought up, or the record doesn't parse)."""
    try:
        raw = json.loads(orchestrator_record_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    backend, url = raw.get("backend"), raw.get("url")
    if not isinstance(backend, str) or not isinstance(url, str) or not backend or not url:
        return None
    return OrchestratorEndpoint(backend=backend, url=url)


def record_orchestrator(backend: str, url: str, root: Path | None = None) -> None:
    """Record `backend`'s orchestrator at `url` as the environment's."""
    if recorded_orchestrator(root) == OrchestratorEndpoint(backend, url):
        return
    path = orchestrator_record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{ORCHESTRATOR_RECORD}.{os.getpid()}")
    tmp.write_text(json.dumps({"backend": backend, "url": url}, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


class NoOrchestrator(OrchestratorClientError):
    """The environment has no orchestrator recorded, or it isn't answering."""


_BRING_UP_HINT = "run `bot-bottle up`"


def no_orchestrator_message() -> str:
    return f"no orchestrator for environment {active_name()}; {_BRING_UP_HINT}"


def _describe(endpoint: OrchestratorEndpoint) -> str:
    return (f"the orchestrator for environment {active_name()} "
            f"({endpoint.backend} at {endpoint.url})")


def env_orchestrator(*, timeout: float = 2.0) -> OrchestratorEndpoint:
    """The environment's recorded orchestrator, checked to answer `/health`.
    Raises `NoOrchestrator` saying which is missing or down."""
    endpoint = recorded_orchestrator()
    if endpoint is None:
        raise NoOrchestrator(no_orchestrator_message())
    if not OrchestratorClient(endpoint.url, timeout=timeout).health():
        raise NoOrchestrator(f"{_describe(endpoint)} isn't answering; {_BRING_UP_HINT}")
    return endpoint


def env_orchestrator_url(*, timeout: float = 2.0) -> str:
    """The URL of the environment's orchestrator (see `env_orchestrator`)."""
    return env_orchestrator(timeout=timeout).url


# --- orchestrators listening on this host, whichever environment's ---


def _candidates(settings: "Settings") -> tuple[list[OrchestratorEndpoint],
                                               list[BackendProbeFailure]]:
    """Each backend's well-known orchestrator address for this environment,
    and the backends that couldn't be probed."""
    candidates: list[OrchestratorEndpoint] = []
    failures: list[BackendProbeFailure] = []
    try:  # docker: loopback publish, live's fixed port or this environment's
        from .lifecycle import DEFAULT_PORT as _DOCKER_PORT
        if is_live():
            candidates.append(OrchestratorEndpoint("docker", f"http://127.0.0.1:{_DOCKER_PORT}"))
        else:
            # Never live's 8099: a named environment must not reach live.
            from ..backend.docker.orchestrator import recorded_orchestrator_port
            if port := recorded_orchestrator_port():
                candidates.append(OrchestratorEndpoint("docker", f"http://127.0.0.1:{port}"))
    except Exception as exc:  # noqa: BLE001 — backend optional
        failures.append(_probe_failure("docker", exc))
        if is_live():
            candidates.append(OrchestratorEndpoint("docker", "http://127.0.0.1:8099"))
    try:  # firecracker: orchestrator TAP; only on Linux, where probing it
        # elsewhere just waits out the timeout
        from ..backend.firecracker import netpool
        from ..backend.firecracker.infra_vm import ORCHESTRATOR_PORT
        from ..backend.firecracker.util import is_linux
        if is_linux():
            orch_ip = netpool.from_settings(settings.infra).orch_slot().guest_ip
            candidates.append(OrchestratorEndpoint(
                "firecracker", f"http://{orch_ip}:{ORCHESTRATOR_PORT}"))
    except Exception as exc:  # noqa: BLE001 — backend optional / not firecracker
        failures.append(_probe_failure("firecracker", exc))
    try:  # macOS: host-only address
        from ..backend.macos_container.infra import probe_orchestrator_url
        url = probe_orchestrator_url()
        if url:
            candidates.append(OrchestratorEndpoint("macos-container", url))
    except Exception as exc:  # noqa: BLE001 — backend optional / not macOS
        failures.append(_probe_failure("macos-container", exc))
    return candidates, failures


def listening_orchestrators(settings: "Settings", *,
                            timeout: float = 2.0) -> list[OrchestratorEndpoint]:
    """Every bot-bottle orchestrator answering `/health` at a backend's
    well-known address for this environment. Only for diagnosing strays
    (doctor, status) and `for_bottle`'s legacy fallback; to talk to the
    environment's orchestrator, use `env_orchestrator`."""
    candidates, _ = _candidates(settings)
    return [c for c in candidates if OrchestratorClient(c.url, timeout=timeout).health()]


def stray_orchestrators(settings: "Settings", *,
                        timeout: float = 2.0) -> list[OrchestratorEndpoint]:
    """Listening orchestrators that aren't the environment's recorded one
    (every one, when none is recorded)."""
    recorded = recorded_orchestrator()
    return [o for o in listening_orchestrators(settings, timeout=timeout)
            if recorded is None or o.url != recorded.url]


def stray_removal_hint(stray: OrchestratorEndpoint) -> str:
    """How to remove an orchestrator that isn't the environment's."""
    if stray.backend == "docker":
        # pylint: disable-next=import-outside-toplevel
        from ..backend.docker.infra import GATEWAY_NAME, ORCHESTRATOR_NAME
        return ("If nothing else uses it (a leftover test run, say), remove it: "
                f"`docker rm -f {ORCHESTRATOR_NAME} {GATEWAY_NAME}`")
    return (f"If nothing else uses it, stop it: `bot-bottle down --backend {stray.backend}` "
            "(also stops the host controller; `bot-bottle up` starts it again)")


def _why(e: OrchestratorClientError) -> str:
    if e.status in (401, 403):
        return f"rejected this host's key (HTTP {e.status})"
    if e.status is None:
        return f"unreachable ({e})"
    return str(e)


def first_accepting(urls: list[str], call: Callable[[OrchestratorClient], bool], *,
                    what: str) -> str:
    """Run `call` on each orchestrator in turn until one accepts; return its
    URL. `call` returns False when the orchestrator doesn't know the subject
    (a 404). Any rejection — 401/403 (a different key), 404, another error
    status, unreachable — means "not this one" and the next is tried. Raises,
    listing each URL and why, when none accepted."""
    reasons: list[str] = []
    for url in urls:
        try:
            if call(OrchestratorClient(url)):
                return url
            reasons.append(f"{url}: doesn't know {what} (HTTP 404)")
        except OrchestratorClientError as e:
            reasons.append(f"{url}: {_why(e)}")
    raise OrchestratorClientError(
        f"no orchestrator accepted {what}: " + ("; ".join(reasons) or "none to try"))


def for_bottle(call: Callable[[OrchestratorClient], bool], *, what: str,
               settings: "Settings | None" = None) -> str:
    """Run a bottle-addressed `call` (returning False for an unknown bottle)
    on the orchestrator that registered the bottle: the environment's. Returns
    its URL; raises naming it when it's unreachable, rejects, or doesn't know
    `what`.

    Only when the environment has no orchestrator recorded (its infra was
    brought up before environments recorded theirs) are the listening
    orchestrators tried in turn (`first_accepting`)."""
    endpoint = recorded_orchestrator()
    if endpoint is not None:
        try:
            if call(OrchestratorClient(endpoint.url)):
                return endpoint.url
        except OrchestratorClientError as e:
            if e.status is None:
                raise NoOrchestrator(
                    f"{_describe(endpoint)} is unreachable ({e}); {_BRING_UP_HINT}") from e
            raise OrchestratorClientError(f"{_describe(endpoint)}: {_why(e)}",
                                          status=e.status) from e
        raise OrchestratorClientError(f"{_describe(endpoint)} doesn't know {what}", status=404)
    urls = [o.url for o in listening_orchestrators(settings or Settings())]
    if not urls:
        raise NoOrchestrator(no_orchestrator_message())
    return first_accepting(urls, call, what=what)


# The key `pending_proposals` adds to each proposal: the orchestrator holding it.
ORCHESTRATOR_KEY = "orchestrator_url"


def pending_proposals() -> list[dict[str, object]]:
    """Pending proposals from the environment's orchestrator, each tagged with
    its URL (`ORCHESTRATOR_KEY`) so the answer goes back there."""
    url = env_orchestrator_url()
    return [{**p, ORCHESTRATOR_KEY: url} for p in OrchestratorClient(url).supervise_pending()]


__all__ = [
    "OrchestratorClient",
    "OrchestratorClientError",
    "NoOrchestrator",
    "OrchestratorEndpoint",
    "RegisteredBottle",
    "DEFAULT_TIMEOUT_SECONDS",
    "ORCHESTRATOR_KEY",
    "env_orchestrator",
    "env_orchestrator_url",
    "first_accepting",
    "for_bottle",
    "listening_orchestrators",
    "no_orchestrator_message",
    "orchestrator_record_path",
    "pending_proposals",
    "record_orchestrator",
    "recorded_orchestrator",
    "stray_orchestrators",
    "stray_removal_hint",
]
