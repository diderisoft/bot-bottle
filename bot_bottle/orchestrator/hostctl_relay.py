"""The orchestrator as a client of the host controller (PRD 0092).

Remote launches and attaches reach a bottle through the host controller,
which the orchestrator calls with the `orchestrator`-role token it was given
at bring-up (it never holds the host controller's key). Stdlib only.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import BinaryIO

from ..contract.hostctl import (
    HOST_CONTROL, HOSTCTL_URL_ENV, LIVE_BOTTLES_PATH, SESSIONS_PATH, bearer,
)
from ..lib import websocket as ws

REQUEST_TIMEOUT_SECONDS = 30.0


class HostControlUnreachable(Exception):
    """The host controller didn't answer."""


class LiveBottlesUnknown(Exception):
    """The host controller couldn't say which bottles are running."""


class HostControlRefused(Exception):
    """The host controller answered a streamed request with an error status."""

    def __init__(self, status: int, body: dict[str, object]) -> None:
        super().__init__(f"host controller: {status}")
        self.status = status
        self.body = body


@dataclass(frozen=True)
class HostControlRelay:
    """Calls the host controller at `url` as the `orchestrator` role."""

    url: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HostControlRelay | None":
        """The relay the orchestrator's bring-up provisioned, or None when it
        wasn't brought up by a host controller."""
        env = os.environ if environ is None else environ
        url = env.get(HOSTCTL_URL_ENV, "").strip().rstrip("/")
        token = env.get(HOST_CONTROL.token_env, "").strip()
        return cls(url, token) if url and token else None

    def request(self, method: str, path: str,
                body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        """(status, JSON body) of one call; error statuses are returned, not raised."""
        data = None if body is None else json.dumps(body).encode()
        headers = bearer(self.token)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read())
        except OSError as e:
            raise HostControlUnreachable(f"host controller unreachable: {e}") from None

    def live_source_ips(self) -> list[str]:
        """The source IPs of the bottles running on this orchestrator's
        backend (`GET /bottles/live`; its listener narrows it to that
        backend). Raises `LiveBottlesUnknown` unless the answer is complete:
        an unreachable daemon, an error status or a backend it couldn't list."""
        try:
            status, body = self.request("GET", LIVE_BOTTLES_PATH)
        except HostControlUnreachable as e:
            raise LiveBottlesUnknown(str(e)) from None
        bottles, errors = body.get("bottles"), body.get("errors")
        if status != 200 or not isinstance(bottles, list):
            raise LiveBottlesUnknown(
                f"host controller: {status}: {body.get('error', 'no bottle list')}")
        if errors:
            raise LiveBottlesUnknown(f"host controller couldn't list bottles: {errors}")
        return [b["source_ip"] for b in bottles
                if isinstance(b, dict) and isinstance(b.get("source_ip"), str) and b["source_ip"]]

    def stream(self, path: str) -> Iterator[bytes]:
        """The lines of a streamed response, as they arrive (no read timeout:
        a build step can be quiet for minutes). Opens it before returning, so
        a refusal raises here rather than mid-stream."""
        req = urllib.request.Request(self.url + path, headers=bearer(self.token))
        try:
            resp = urllib.request.urlopen(req, timeout=None)  # pylint: disable=consider-using-with
        except urllib.error.HTTPError as e:
            raise HostControlRefused(e.code, _json(e.read())) from None
        except OSError as e:
            raise HostControlUnreachable(f"host controller unreachable: {e}") from None
        return _lines(resp)

    def attach(self, session_id: str, *, cols: int, rows: int
               ) -> tuple[socket.socket, BinaryIO, BinaryIO]:
        """Open the host controller's attach WebSocket for `session_id`."""
        parts = urllib.parse.urlsplit(self.url)
        path = (f"{SESSIONS_PATH}/{urllib.parse.quote(session_id, safe='')}/attach"
                f"?cols={cols}&rows={rows}")
        try:
            sock, reader, writer = ws.connect(parts.hostname or "", parts.port or 80, path,
                                              headers=bearer(self.token))
        except OSError as e:
            raise HostControlUnreachable(f"host controller unreachable: {e}") from None
        sock.settimeout(None)  # a session can be quiet for as long as it likes
        return sock, reader, writer


def _lines(resp: http.client.HTTPResponse) -> Iterator[bytes]:
    with resp:
        yield from resp


def _json(raw: bytes) -> dict[str, object]:
    try:
        body = json.loads(raw)
    except ValueError:
        return {"error": raw.decode(errors="replace")[:300]}
    return body if isinstance(body, dict) else {"value": body}


__all__ = ["HostControlRefused", "HostControlRelay", "HostControlUnreachable",
           "LiveBottlesUnknown"]
