"""Where `log: 2` (full) egress logging writes request and response bodies.

Not stderr: on Firecracker that is the gateway VM's serial console, which a few
busy agents' bodies can saturate (stalling every bottle's traffic) and which
keeps whole conversations in a log the host can read. Full entries go to a
size-capped, rotated file instead, created 0600 in `EGRESS_FULL_LOG_DIR`
(default `/var/log/bot-bottle-egress`), each body cut at `BODY_LIMIT_BYTES`.
At most `MAX_BYTES * (BACKUPS + 1)` is kept; the oldest file is dropped.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import typing

from .dlp_detectors import redact_tokens

if typing.TYPE_CHECKING:
    from mitmproxy import http  # type: ignore[import-not-found]  # pylint: disable=import-error

DEFAULT_DIR = "/var/log/bot-bottle-egress"
FILE_NAME = "full.jsonl"
MAX_BYTES = 16 * 1024 * 1024
BACKUPS = 3
BODY_LIMIT_BYTES = 64 * 1024


def _body(text: str) -> dict[str, object]:
    """The body, cut at `BODY_LIMIT_BYTES`, with its full length."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= BODY_LIMIT_BYTES:
        return {"body": text}
    return {"body": raw[:BODY_LIMIT_BYTES].decode("utf-8", errors="ignore"),
            "body_bytes": len(raw), "body_truncated": True}


class FullLog:
    """An append-only JSON-lines file that rotates at `max_bytes`."""

    def __init__(self, directory: str, *, max_bytes: int = MAX_BYTES,
                 backups: int = BACKUPS) -> None:
        self.path = os.path.join(directory, FILE_NAME)
        self._max_bytes, self._backups = max_bytes, backups
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: "typing.Mapping[str, str]") -> "FullLog":
        """In `EGRESS_FULL_LOG_DIR` (or the default), else a private temp dir
        when that can't be created."""
        directory = env.get("EGRESS_FULL_LOG_DIR", "").strip() or DEFAULT_DIR
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as e:
            fallback = os.path.join(tempfile.gettempdir(), "bot-bottle-egress")
            sys.stderr.write(f"egress: full log dir {directory!r} unusable ({e}); "
                             f"using {fallback}\n")
            os.makedirs(fallback, mode=0o700, exist_ok=True)
            directory = fallback
        return cls(directory)

    def write(self, entry: dict[str, object]) -> None:
        """Append one entry. Never raises: a full log must not break a request."""
        line = (json.dumps(entry) + "\n").encode("utf-8")
        with self._lock:
            try:
                self._rotate_if_needed(len(line))
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError as e:
                sys.stderr.write(f"egress: full log write failed: {e}\n")

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size + incoming <= self._max_bytes:
            return
        for i in range(self._backups, 0, -1):
            src = self.path if i == 1 else f"{self.path}.{i - 1}"
            if os.path.exists(src):
                os.replace(src, f"{self.path}.{i}")
        if self._backups == 0:
            os.remove(self.path)

    def request(self, flow: "http.HTTPFlow", env: "typing.Mapping[str, str]") -> None:
        """The request as sent upstream, minus `Authorization`; the flow's
        env, so the calling bottle's secrets are redacted."""
        headers = {k: redact_tokens(v, env=env) for k, v in flow.request.headers.items()
                   if k.lower() != "authorization"}
        self.write({
            "event": "egress_request",
            "host": redact_tokens(flow.request.host, env=env),
            "method": flow.request.method,
            "path": redact_tokens(flow.request.path, env=env),
            "headers": headers,
            **_body(redact_tokens(flow.request.get_text(strict=False) or "", env=env)),
        })

    def response(self, flow: "http.HTTPFlow", env: "typing.Mapping[str, str]") -> None:
        """The response, redacted with the same per-flow env."""
        assert flow.response is not None
        headers = {k: redact_tokens(v, env=env) for k, v in flow.response.headers.items()}
        self.write({
            "event": "egress_response",
            "host": flow.request.host,
            "status": flow.response.status_code,
            "headers": headers,
            **_body(redact_tokens(flow.response.get_text(strict=False) or "", env=env)),
        })
