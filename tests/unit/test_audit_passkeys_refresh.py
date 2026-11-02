"""`bot-bottle audit passkeys --trust` refreshes the host's short-lived console
access token before fetching, as `pair on` does (a stale one got a 401)."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import audit_passkeys


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TestFreshAccessToken(unittest.TestCase):
    def _console(self, **extra: Any) -> dict[str, Any]:
        return {"url": "https://console.example/", "access_token": "old", **extra}

    def test_refreshes_and_saves(self) -> None:
        console = self._console(refresh_token="r")
        seen: list[Any] = []

        def fake_open(req: Any, timeout: float = 0) -> _Resp:
            seen.append((req.full_url, json.loads(req.data)))
            return _Resp(json.dumps({"access_token": "new"}).encode())

        with mock.patch("urllib.request.urlopen", fake_open), \
                mock.patch.object(audit_passkeys.pairing, "save_console") as saved:
            token = audit_passkeys.fresh_access_token(console)
        self.assertEqual("new", token)
        self.assertEqual([("https://console.example/api/v1/hosts/token/refresh",
                           {"refresh_token": "r"})], seen)
        saved.assert_called_once()
        self.assertEqual("new", saved.call_args.args[0]["access_token"])

    def test_a_failed_refresh_keeps_the_stored_token(self) -> None:
        def refused(req: Any, timeout: float = 0) -> _Resp:
            raise urllib.error.URLError("down")

        with mock.patch("urllib.request.urlopen", refused), \
                mock.patch.object(audit_passkeys.pairing, "save_console") as saved:
            token = audit_passkeys.fresh_access_token(self._console(refresh_token="r"))
        self.assertEqual("old", token)
        saved.assert_not_called()

    def test_without_a_refresh_token_it_uses_the_stored_one(self) -> None:
        with mock.patch("urllib.request.urlopen") as opened:
            self.assertEqual("old", audit_passkeys.fresh_access_token(self._console()))
        opened.assert_not_called()

    def test_trust_uses_the_fresh_token(self) -> None:
        console = self._console(refresh_token="r")
        with mock.patch.object(audit_passkeys.pairing, "load_console", return_value=console), \
                mock.patch.object(audit_passkeys, "fresh_access_token", return_value="new"), \
                mock.patch.object(audit_passkeys.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(audit_passkeys, "trust_from_console", return_value=0) as trust:
            self.assertEqual(0, audit_passkeys.run(trust=True))
        trust.assert_called_once_with("https://console.example/", "new")


if __name__ == "__main__":
    unittest.main()
