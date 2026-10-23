"""Unit: OpenRouter sign-in (OAuth PKCE) and the free-model lookup."""

from __future__ import annotations

import base64
import hashlib
import threading
import unittest
import urllib.parse
import urllib.request
from typing import Any
from unittest.mock import patch

from bot_bottle.cli import openrouter


def _model(model_id: str, *, free: bool = True, tools: bool = True, **extra: Any) -> dict[str, Any]:
    price = "0" if free else "0.000001"
    return {"id": model_id, "pricing": {"prompt": price, "completion": price},
            "supported_parameters": ["tools"] if tools else [], **extra}


class FreeModels(unittest.TestCase):
    def test_keeps_free_tool_capable_models(self) -> None:
        data = {"data": [
            _model("a/x:free", context_length=1000, top_provider={"max_completion_tokens": 50}),
            _model("b/y:free", tools=False),
            _model("c/z", free=False),
            _model("d/w"),  # zero-priced without the suffix
        ]}
        models = openrouter.free_tool_models(lambda _url: data)
        self.assertEqual(["a/x:free", "d/w"], [m.id for m in models])
        self.assertEqual((1000, 50), (models[0].context_length, models[0].max_completion_tokens))

    def test_the_router_comes_first_then_ranked_models(self) -> None:
        ranked = openrouter.RANKED_FREE_MODELS[1]
        listed = [openrouter.FreeModel("other:free"), openrouter.FreeModel(ranked),
                  openrouter.FreeModel(openrouter.FREE_ROUTER, 200000)]
        self.assertEqual([openrouter.FREE_ROUTER, ranked],
                         [m.id for m in openrouter.candidates(listed)])
        self.assertEqual(200000, openrouter.pick_free_model(listed).context_length)

    def test_probe_sends_a_tool_request_with_the_key(self) -> None:
        sent: list[tuple[str, dict[str, Any], str]] = []

        def post(url: str, body: dict[str, Any], key: str) -> Any:
            sent.append((url, body, key))
            return {"choices": [{"message": {"content": "OK"}}]}

        self.assertTrue(openrouter.probe("m:free", "k", post=post))
        url, body, key = sent[0]
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(("m:free", "k"), (body["model"], key))
        self.assertTrue(body["tools"])

    def test_probe_fails_on_a_provider_error(self) -> None:
        def post(_u: str, _b: dict[str, Any], _k: str) -> Any:
            raise OSError("HTTP Error 404")
        self.assertFalse(openrouter.probe("m:free", "k", post=post))

    def test_the_first_answering_candidate_wins(self) -> None:
        ranked = openrouter.RANKED_FREE_MODELS
        listed = [openrouter.FreeModel(ranked[0]), openrouter.FreeModel(ranked[2])]
        tried: list[str] = []

        def check(model_id: str, _key: str) -> bool:
            tried.append(model_id)
            return model_id == ranked[2]

        self.assertEqual(ranked[2], openrouter.pick_working_model(listed, "k", check=check).id)
        self.assertEqual([openrouter.FREE_ROUTER, ranked[0], ranked[2]], tried)

    def test_a_failed_listing_is_an_openrouter_error(self) -> None:
        def boom(_url: str) -> Any:
            raise OSError("offline")
        with self.assertRaises(openrouter.OpenRouterError):
            openrouter.free_tool_models(boom)


class SignIn(unittest.TestCase):
    def test_auth_url_carries_an_s256_challenge(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(
            openrouter.auth_url("chal", "http://localhost:1/callback")).query)
        self.assertEqual({"code_challenge": ["chal"], "code_challenge_method": ["S256"],
                          "callback_url": ["http://localhost:1/callback"]}, query)

    def test_the_verifier_matches_the_challenge(self) -> None:
        verifier, challenge = openrouter._pkce()  # pylint: disable=protected-access
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.assertEqual(digest.rstrip(b"=").decode(), challenge)

    def test_exchange_posts_code_and_verifier(self) -> None:
        sent: list[dict[str, Any]] = []
        key = openrouter.exchange_code("c", "v", post=lambda _u, b: sent.append(b) or {"key": "k"})
        self.assertEqual("k", key)
        self.assertEqual({"code": "c", "code_verifier": "v", "code_challenge_method": "S256"},
                         sent[0])

    def test_headless_sign_in_takes_a_pasted_code(self) -> None:
        with patch.object(openrouter, "exchange_code", return_value="k") as exchange:
            key = openrouter.sign_in(browser=False, say=lambda _s: None, ask=lambda _q: " c0de ")
        self.assertEqual("k", key)
        self.assertEqual("c0de", exchange.call_args.args[0])

    def test_browser_sign_in_catches_the_callback_on_localhost(self) -> None:
        def browser(url: str) -> bool:
            callback = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["callback_url"][0]
            port = urllib.parse.urlsplit(callback).port
            threading.Thread(target=lambda: urllib.request.urlopen(  # pylint: disable=consider-using-with
                f"http://127.0.0.1:{port}/callback?code=abc", timeout=5).read()).start()
            return True

        with patch("webbrowser.open", side_effect=browser), \
                patch.object(openrouter, "exchange_code", return_value="k") as exchange:
            key = openrouter.sign_in(say=lambda _s: None, timeout=10)
        self.assertEqual("k", key)
        self.assertEqual("abc", exchange.call_args.args[0])

    def test_browser_sign_in_times_out(self) -> None:
        with patch("webbrowser.open", return_value=True), \
                self.assertRaises(openrouter.OpenRouterError):
            openrouter.sign_in(say=lambda _s: None, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
