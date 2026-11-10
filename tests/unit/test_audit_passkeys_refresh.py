"""`bot-bottle audit passkeys --trust` refreshes the host's short-lived console
access token before fetching, as `pair on` does (a stale one got a 401)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import audit_passkeys
from bot_bottle.cli.host_state_client import pairing, set_pairing
from bot_bottle.control.hostctl.pairing import Pairing
from tests.unit import serve_host_controller, use_bottle_root


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_urlopen = urllib.request.urlopen


def _console_only(fake: Any) -> Any:
    """`fake` for requests to the console; the host controller's go through."""
    def opener(req: Any, timeout: float | None = None) -> Any:
        if req.full_url.startswith("https://console.example/"):
            return fake(req, timeout)
        return _urlopen(req, timeout=timeout)
    return opener


class TestFreshAccessToken(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))
        serve_host_controller(self)

    def _paired(self, **tokens: str) -> Pairing:
        return set_pairing(new=True, url="https://console.example/", access_token="old",
                           **tokens)

    def test_refreshes_and_saves(self) -> None:
        paired = self._paired(refresh_token="r")
        seen: list[Any] = []

        def fake_open(req: Any, timeout: float = 0) -> _Resp:
            seen.append((req.full_url, json.loads(req.data)))
            return _Resp(json.dumps({"access_token": "new"}).encode())

        with mock.patch("urllib.request.urlopen", _console_only(fake_open)):
            token = audit_passkeys.fresh_access_token(paired)
        self.assertEqual("new", token)
        self.assertEqual([("https://console.example/api/v1/hosts/token/refresh",
                           {"refresh_token": "r"})], seen)
        self.assertEqual(("new", "r"), pairing()[0].tokens())

    def test_a_failed_refresh_keeps_the_stored_token(self) -> None:
        def refused(req: Any, timeout: float = 0) -> _Resp:
            raise urllib.error.URLError("down")

        with mock.patch("urllib.request.urlopen", _console_only(refused)):
            token = audit_passkeys.fresh_access_token(self._paired(refresh_token="r"))
        self.assertEqual("old", token)
        self.assertEqual(("old", "r"), pairing()[0].tokens())

    def test_without_a_refresh_token_it_uses_the_stored_one(self) -> None:
        paired = self._paired()
        with mock.patch("urllib.request.urlopen") as opened:
            self.assertEqual("old", audit_passkeys.fresh_access_token(paired))
        opened.assert_not_called()

    def test_trust_uses_the_fresh_token(self) -> None:
        self._paired(refresh_token="r")
        with mock.patch.object(audit_passkeys, "fresh_access_token", return_value="new"), \
                mock.patch.object(audit_passkeys.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(audit_passkeys, "trust_from_console", return_value=0) as trust:
            self.assertEqual(0, audit_passkeys.run(trust=True))
        trust.assert_called_once_with("https://console.example/", "new")


if __name__ == "__main__":
    unittest.main()
