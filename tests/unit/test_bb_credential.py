"""Tests for the in-bottle `bb-credential` script (no real HTTP)."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest import mock

from bot_bottle.agent_scripts import bb_credential


class _Response:
    """A stand-in for `urlopen`'s response context manager."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestBbCredentialScript(unittest.TestCase):

    def _run(self, args: list[str], *, proxy: str = "",
             response: dict[str, Any] | None = None,
             status: int = 200) -> tuple[int, str, str, list[urllib.request.Request]]:
        """Run `bb_credential.main()`; return (rc, stdout, stderr, requests)."""
        seen: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: float = 10) -> _Response:
            seen.append(req)
            if status == 404:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found",
                                             mock.Mock(), None)
            return _Response(response or {"values": {"token": "test-tok"}})

        out, err = io.StringIO(), io.StringIO()
        env = {"HTTP_PROXY": proxy} if proxy else {}
        rc = 0
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch("sys.argv", ["bb-credential", *args]), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                bb_credential.main()
            except SystemExit as e:
                rc = int(e.code or 0)
        return rc, out.getvalue(), err.getvalue(), seen

    def test_get_first_value(self) -> None:
        rc, out, _, _ = self._run(["get", "my-cred"])
        self.assertEqual(rc, 0)
        self.assertIn("test-tok", out)

    def test_get_specific_field(self) -> None:
        rc, out, _, _ = self._run(["get", "my-cred", "token"],
                                  response={"values": {"token": "field-val"}})
        self.assertEqual(rc, 0)
        self.assertIn("field-val", out)

    def test_get_missing_field_exits_1(self) -> None:
        rc, _, err, _ = self._run(["get", "my-cred", "nonexistent"],
                                  response={"values": {"token": "x"}})
        self.assertEqual(rc, 1)
        self.assertIn("nonexistent", err)

    def test_aws_subcommand_outputs_credential_process_json(self) -> None:
        rc, out, _, _ = self._run(["aws", "my-aws-cred"], response={
            "values": {"access_key_id": "AKID", "secret_access_key": "SAK",
                       "session_token": "ST"},
            "expires_at": "2026-11-14T13:00:00Z",
        })
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["Version"], 1)
        self.assertEqual(payload["AccessKeyId"], "AKID")
        self.assertEqual(payload["SessionToken"], "ST")
        self.assertIn("Expiration", payload)

    def test_k8s_subcommand_outputs_exec_credential_json(self) -> None:
        rc, out, _, _ = self._run(["k8s", "my-k8s-cred"], response={
            "values": {"token": "k8s-tok"}, "expires_at": "2026-11-14T13:00:00Z"})
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "ExecCredential")
        self.assertEqual(payload["status"]["token"], "k8s-tok")

    def test_404_exits_1(self) -> None:
        rc, _, err, _ = self._run(["get", "missing-cred"], status=404)
        self.assertEqual(rc, 1)
        self.assertIn("missing-cred", err)

    def test_no_subcommand_exits_2(self) -> None:
        rc, _, _, _ = self._run([])
        self.assertEqual(rc, 2)

    def test_unknown_subcommand_exits_2(self) -> None:
        rc, _, _, _ = self._run(["badcmd"])
        self.assertEqual(rc, 2)

    def test_identity_token_sent_from_proxy(self) -> None:
        """The proxy URL's password is sent as `x-bot-bottle-identity`."""
        _, _, _, seen = self._run(["get", "cred"],
                                  proxy="http://user:mytoken@egress:3128")
        self.assertTrue(seen)
        self.assertEqual(seen[0].get_header("X-bot-bottle-identity"), "mytoken")


if __name__ == "__main__":
    unittest.main()
