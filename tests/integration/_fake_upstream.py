"""A capture-and-echo HTTP server used as a fake upstream behind the
cred-proxy in integration tests.

Captures the last request's method, path, and headers under
/__last_request (as JSON). Returns a fixed 200 OK with a deterministic
body for every other path. Tests probe /__last_request to assert on
header injection (PRD 0010 SC3/SC6).

Stdlib-only; runs inside a python:alpine container with a single
bind-mount.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading


_lock = threading.Lock()
_last_request: dict[str, object] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Quiet — the test reads the capture endpoint, not stderr.
        return

    def _capture_and_respond(self) -> None:
        # Skip capturing the inspection endpoints so the test's own
        # query to /__last_request doesn't overwrite the request it
        # came in to inspect.
        if not self.path.startswith("/__"):
            with _lock:
                global _last_request
                _last_request = {
                    "method": self.command,
                    "path": self.path,
                    "headers": [[k, v] for k, v in self.headers.items()],
                }
        if self.path == "/__last_request":
            body = json.dumps(_last_request, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/__sse":
            # SSE-style streaming response. Used by the no-buffering
            # test: three events with short flushes between them.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: event-{i}\n\n".encode("utf-8"))
                self.wfile.flush()
            return
        body = b'{"upstream":"fake","ok":true}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None: self._capture_and_respond()
    def do_POST(self) -> None: self._capture_and_respond()
    def do_PUT(self) -> None: self._capture_and_respond()
    def do_DELETE(self) -> None: self._capture_and_respond()
    def do_PATCH(self) -> None: self._capture_and_respond()


class FakeServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    port = int(os.environ.get("FAKE_UPSTREAM_PORT", "8080"))
    server = FakeServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"fake-upstream listening on :{port}\n")
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
