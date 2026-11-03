"""An in-process OCI registry for tests: the `/v2/` subset `oci_registry` uses,
behind a Bearer token realm like GHCR's. Anonymous tokens can pull; pushing
needs the Basic credentials `user`/`secret`. Blob GETs redirect to a "CDN"
path that refuses any Authorization header, as a signed CDN URL would."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USER, SECRET = "user", "secret"

_V2 = re.compile(r"^/v2/(?P<repo>.+)/(?P<kind>blobs|manifests)/(?P<ref>[^/]+)$")
_UPLOADS = re.compile(r"^/v2/(?P<repo>.+)/blobs/uploads/$")


class FakeRegistry:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.uploads: dict[str, str] = {}
        self.uploaded: list[str] = []
        self.cdn_auth_seen = False
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
                del format, args

            def _send(self, code: int, body: bytes = b"", headers: dict[str, str] | None = None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _authorized(self, repo: str, push: bool) -> bool:
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return False
                actions = auth.removeprefix("Bearer ").split("|")
                return actions[0] == repo and (not push or "push" in actions[1])

            def _challenge(self, repo: str) -> None:
                realm = f"http://{self.headers['Host']}/token"
                self._send(401, b"{}", {"WWW-Authenticate":
                    f'Bearer realm="{realm}",service="fake",scope="repository:{repo}:pull"'})

            def _token(self) -> None:
                scope = parse_qs(urlsplit(self.path).query)["scope"][0]
                _, repo, actions = scope.split(":")
                if "push" in actions:
                    want = base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
                    if self.headers.get("Authorization") != f"Basic {want}":
                        self._send(401, b"{}")
                        return
                self._send(200, json.dumps({"token": f"{repo}|{actions}"}).encode())

            def do_GET(self) -> None:  # noqa: N802
                self._read()

            def do_HEAD(self) -> None:  # noqa: N802
                self._read()

            def _read(self) -> None:
                path = urlsplit(self.path).path
                if path == "/token":
                    self._token()
                    return
                if path.startswith("/cdn/"):
                    registry.cdn_auth_seen |= "Authorization" in self.headers
                    digest = path.removeprefix("/cdn/")
                    self._send(200, registry.blobs[digest])
                    return
                m = _V2.match(path)
                if not m:
                    self._send(404)
                    return
                repo, kind, ref = m["repo"], m["kind"], m["ref"]
                if not self._authorized(repo, push=False):
                    self._challenge(repo)
                    return
                if kind == "blobs":
                    if ref not in registry.blobs:
                        self._send(404)
                    elif self.command == "GET":
                        self._send(307, headers={"Location": f"/cdn/{ref}"})
                    else:
                        self._send(200)
                    return
                found = registry.manifests.get((repo, ref))
                if found is None:
                    self._send(404)
                    return
                body, media = found
                self._send(200, body, {"Content-Type": media,
                                       "Docker-Content-Digest": _digest(body)})

            def do_POST(self) -> None:  # noqa: N802
                m = _UPLOADS.match(urlsplit(self.path).path)
                self._body()
                if not m:
                    self._send(404)
                    return
                if not self._authorized(m["repo"], push=True):
                    self._challenge(m["repo"])
                    return
                upload = uuid.uuid4().hex
                registry.uploads[upload] = m["repo"]
                self._send(202, headers={"Location": f"/v2/{m['repo']}/blobs/uploads/{upload}"})

            def do_PUT(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                body = self._body()
                upload = parts.path.rsplit("/", 1)[-1]
                if upload in registry.uploads:
                    if not self._authorized(registry.uploads[upload], push=True):
                        self._challenge(registry.uploads[upload])
                        return
                    digest = parse_qs(parts.query)["digest"][0]
                    if _digest(body) != digest:
                        self._send(400)
                        return
                    registry.blobs[digest] = body
                    registry.uploaded.append(digest)
                    self._send(201)
                    return
                m = _V2.match(parts.path)
                if not m or m["kind"] != "manifests":
                    self._send(404)
                    return
                if not self._authorized(m["repo"], push=True):
                    self._challenge(m["repo"])
                    return
                media = self.headers.get("Content-Type", "")
                registry.manifests[(m["repo"], m["ref"])] = (body, media)
                registry.manifests[(m["repo"], _digest(body))] = (body, media)
                self._send(201, headers={"Docker-Content-Digest": _digest(body)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> "FakeRegistry":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _digest(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"
