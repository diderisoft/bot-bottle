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
             status: int = 200, prog: str = "bb-credential", stdin: str = "",
             env: dict[str, str] | None = None,
             fail: Exception | None = None) -> tuple[int, str, str, list[urllib.request.Request]]:
        """Run `bb_credential.main()`; return (rc, stdout, stderr, requests)."""
        seen: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, timeout: float = 10) -> _Response:
            seen.append(req)
            if fail is not None:
                raise fail
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "error",
                                             mock.Mock(), None)
            return _Response(response if response is not None
                             else {"values": {"token": "test-tok"}})

        out, err = io.StringIO(), io.StringIO()
        environ = {"HTTP_PROXY": proxy, "http_proxy": "", "BB_CRED_LABEL": "", **(env or {})}
        rc = 0
        with mock.patch.dict("os.environ", environ, clear=False), \
             mock.patch("sys.argv", [prog, *args]), \
             mock.patch("sys.stdin", io.StringIO(stdin)), \
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


    def test_gateway_error_exits_1(self) -> None:
        rc, _, err, _ = self._run(["get", "c"], status=500)
        self.assertEqual(rc, 1)
        self.assertIn("gateway error 500", err)

    def test_unreachable_gateway_exits_1(self) -> None:
        rc, _, err, _ = self._run(["get", "c"], fail=OSError("refused"))
        self.assertEqual(rc, 1)
        self.assertIn("could not reach gateway", err)

    def test_default_gateway_without_proxy(self) -> None:
        _, _, _, seen = self._run(["get", "my cred"])
        self.assertEqual(seen[0].full_url, "http://egress:%d/bb-credential/my%%20cred"
                         % bb_credential._SUPERVISE_PORT)  # pylint: disable=protected-access
        self.assertIsNone(seen[0].get_header("X-bot-bottle-identity"))

    def test_get_without_label_exits_2(self) -> None:
        self.assertEqual(self._run(["get"])[0], 2)

    def test_get_with_no_values_exits_1(self) -> None:
        rc, _, err, _ = self._run(["get", "c"], response={"values": {}})
        self.assertEqual(rc, 1)
        self.assertIn("has no values", err)

    def test_aws_without_label_exits_2(self) -> None:
        self.assertEqual(self._run(["aws"])[0], 2)

    def test_k8s_without_label_exits_2(self) -> None:
        self.assertEqual(self._run(["k8s"])[0], 2)

    def test_k8s_expiry(self) -> None:
        _, out, _, _ = self._run(["k8s", "c"], response={
            "values": {"token": "t"}, "expires_at": "2026-11-14T13:00:00Z"})
        self.assertEqual(json.loads(out)["status"]["expirationTimestamp"],
                         "2026-11-14T13:00:00Z")

    def test_git_get_serves_the_username_label(self) -> None:
        rc, out, _, seen = self._run(
            ["git", "get"], stdin="protocol=https\nhost=g.example.com\nusername=gh-tok\n\n",
            response={"values": {"token": "secret-tok"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "username=gh-tok\npassword=secret-tok\n")
        self.assertTrue(seen[0].full_url.endswith("/bb-credential/gh-tok"))

    def test_git_label_from_env(self) -> None:
        _, out, _, _ = self._run(["git", "get"], stdin="host=g.example.com\n",
                                 env={"BB_CRED_LABEL": "envlabel"})
        self.assertIn("username=envlabel", out)

    def test_git_store_and_erase_are_no_ops(self) -> None:
        for action in ("store", "erase"):
            rc, out, _, seen = self._run(["git", action], stdin="username=x\n")
            self.assertEqual((rc, out, seen), (0, "", []))

    def test_git_without_label_says_nothing(self) -> None:
        rc, out, _, seen = self._run(["git", "get"], stdin="host=g.example.com\n")
        self.assertEqual((rc, out, seen), (0, "", []))

    def test_git_without_token_says_nothing(self) -> None:
        rc, out, _, _ = self._run(["git", "get"], stdin="username=x\n",
                                  response={"values": {"other": "v"}})
        self.assertEqual((rc, out), (0, ""))

    def test_docker_get(self) -> None:
        rc, out, _, _ = self._run(["get"], prog="/usr/local/bin/docker-credential-bb",
                                  stdin="https://registry.example.com\n",
                                  env={"BB_CRED_LABEL": "reg"},
                                  response={"values": {"token": "rtok"}})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"ServerURL": "https://registry.example.com",
                                           "Username": "reg", "Secret": "rtok"})

    def test_docker_get_needs_a_label(self) -> None:
        rc, _, err, _ = self._run([], prog="docker-credential-bb", stdin="https://r\n")
        self.assertEqual(rc, 1)
        self.assertIn("BB_CRED_LABEL", err)

    def test_docker_other_actions_are_no_ops(self) -> None:
        rc, out, _, seen = self._run(["store"], prog="docker-credential-bb")
        self.assertEqual((rc, out, seen), (0, "", []))


if __name__ == "__main__":
    unittest.main()
