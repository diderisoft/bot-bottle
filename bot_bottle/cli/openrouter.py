"""OpenRouter for the pi starter (PRD 0091): sign in from the browser, and
pick a free model.

Sign-in is OpenRouter's OAuth PKCE flow. The user signs in (or signs up) in
their own browser, and the key that comes back belongs to them. With a
browser, the callback lands on a one-shot server on localhost. Without one
(SSH), OpenRouter shows the code and the user pastes it. bot-bottle never
automates signup.

The model is looked up, not pinned, because OpenRouter's free list changes.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import queue
import secrets
import socket
import threading
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

API = "https://openrouter.ai/api/v1"
AUTH_URL = "https://openrouter.ai/auth"
FREE_ROUTER = "openrouter/free"

# Free models known to handle agentic coding, best first. The first one that
# is listed (free, with tools) wins; `openrouter/free` is the fallback.
RANKED_FREE_MODELS = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
)


class OpenRouterError(Exception):
    """Sign-in or the model lookup failed; the message is safe to show."""


@dataclass(frozen=True)
class FreeModel:
    id: str
    context_length: int = 0
    max_completion_tokens: int = 0
    reasoning: bool = False


def _get_json(url: str, timeout: float = 15) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _is_free(model: dict[str, Any]) -> bool:
    pricing = model.get("pricing") or {}
    return str(model.get("id", "")).endswith(":free") or (
        str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0")


def free_tool_models(fetch: Callable[[str], Any] = _get_json) -> list[FreeModel]:
    """OpenRouter's free models that support tool calling (public endpoint,
    no key)."""
    try:
        data = fetch(f"{API}/models")["data"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise OpenRouterError(f"couldn't list OpenRouter's models: {e}") from e
    out: list[FreeModel] = []
    for m in data:
        params = m.get("supported_parameters") or []
        if not isinstance(m, dict) or not _is_free(m) or "tools" not in params:
            continue
        top = m.get("top_provider") or {}
        out.append(FreeModel(
            id=str(m["id"]),
            context_length=int(m.get("context_length") or 0),
            max_completion_tokens=int(top.get("max_completion_tokens") or 0),
            reasoning="reasoning" in params,
        ))
    return out


def pick_free_model(models: list[FreeModel]) -> FreeModel:
    """The best-ranked listed model, else the free router."""
    by_id = {m.id: m for m in models}
    for model_id in RANKED_FREE_MODELS:
        if model_id in by_id:
            return by_id[model_id]
    return by_id.get(FREE_ROUTER, FreeModel(FREE_ROUTER))


# -- sign-in (OAuth PKCE) -------------------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(challenge: str, callback_url: str = "") -> str:
    params = {"code_challenge": challenge, "code_challenge_method": "S256"}
    if callback_url:
        params["callback_url"] = callback_url
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, verifier: str,
                  post: Callable[[str, dict[str, Any]], Any] | None = None) -> str:
    """Trade an authorization code for the user's key."""
    body = {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
    try:
        result = (post or _post_json)(f"{API}/auth/keys", body)
    except (OSError, ValueError) as e:
        raise OpenRouterError(f"OpenRouter didn't accept the sign-in code: {e}") from e
    key = result.get("key") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key:
        raise OpenRouterError("OpenRouter's reply had no key")
    return key


def _post_json(url: str, body: dict[str, Any]) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>bot-bottle</title>"
              b"<body style='font:16px system-ui;margin:3rem'>Signed in to OpenRouter. "
              b"You can close this tab and go back to the terminal.</body>")


def _callback_servers(codes: "queue.Queue[str]") -> tuple[int, list[http.server.HTTPServer]]:
    """One-shot callback servers on localhost, IPv4 and (where available)
    IPv6 on the same port, since a browser may resolve `localhost` to
    either."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if urllib.parse.urlsplit(self.path).path != "/callback" or "code" not in query:
                self.send_error(404)
                return
            codes.put(query["code"][0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE)

        def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
            del format, args

    v4 = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = v4.server_address[1]
    servers = [v4]

    class V6Server(http.server.HTTPServer):
        address_family = socket.AF_INET6

    try:
        servers.append(V6Server(("::1", port), Handler))
    except OSError:
        pass
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, servers


def sign_in(*, browser: bool = True, timeout: float = 300,
            say: Callable[[str], None] = print,
            ask: Callable[[str], str] = input) -> str:
    """Sign the user in to OpenRouter and return their key."""
    verifier, challenge = _pkce()
    if not browser:
        say(f"Open this link, sign in to OpenRouter, then paste the code it shows:\n\n"
            f"  {auth_url(challenge)}\n")
        code = ask("Code: ").strip()
        if not code:
            raise OpenRouterError("no code entered")
        return exchange_code(code, verifier)
    codes: queue.Queue[str] = queue.Queue()
    port, servers = _callback_servers(codes)
    try:
        url = auth_url(challenge, f"http://localhost:{port}/callback")
        say(f"Opening OpenRouter to sign in (free). If no browser opens, visit:\n\n  {url}\n")
        webbrowser.open(url)
        try:
            code = codes.get(timeout=timeout)
        except queue.Empty as e:
            raise OpenRouterError("timed out waiting for the OpenRouter sign-in") from e
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return exchange_code(code, verifier)


def listed(model_id: str, fetch: Callable[[str], Any] = _get_json) -> bool:
    """Whether ``model_id`` is still a free, tool-capable model (`doctor`)."""
    return any(m.id == model_id for m in free_tool_models(fetch))


__all__ = [
    "FREE_ROUTER", "FreeModel", "OpenRouterError", "RANKED_FREE_MODELS",
    "auth_url", "exchange_code", "free_tool_models", "listed", "pick_free_model", "sign_in",
]
