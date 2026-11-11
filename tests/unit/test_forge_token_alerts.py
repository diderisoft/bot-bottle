"""Forge tokens that stopped working for good reach the console once per
failure state (`forge_alerts`, carried by `host_stats`), and git-gate tells an
expired GitHub token from a revoked authorization."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle import forge_alerts
from bot_bottle.backend import provision_gateway
from bot_bottle.contrib.gitea.deploy_key_provisioner import GiteaDeployKeyProvisioner
from bot_bottle.contrib.github.user_token import ReauthorizeError
from bot_bottle.control.git_gate_render import GitGateUpstream, git_gate_render_hook
from bot_bottle.control.hostctl import host_stats
from bot_bottle.control.hostctl.github_git_tokens import GitTokenRenewer
from bot_bottle.deploy_key_provisioner import ForgeTokenRefusedError
from bot_bottle.git_gate import provision
from tests.unit import use_bottle_root

HTTPS = GitGateUpstream(name="design", upstream_url="https://github.com/o/design.git",
                        upstream_host="github.com", upstream_port="443", identity_file="",
                        known_host_key="", transport="https", token_secret="GITHUB_COM_TOKEN")


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://gitea.example/api", code, "no", {},  # type: ignore[arg-type]
                                  io.BytesIO(json.dumps({"message": "token is required"}).encode()))


class TestRegistry(unittest.TestCase):
    def setUp(self) -> None:
        forge_alerts.reset()
        self.addCleanup(forge_alerts.reset)

    def test_once_per_failure_state(self) -> None:
        raise_ = forge_alerts.raise_alert
        self.assertTrue(raise_("T", kind=forge_alerts.GITEA_TOKEN_REFUSED, forge="g", message="a"))
        self.assertFalse(raise_("T", kind=forge_alerts.GITEA_TOKEN_REFUSED, forge="g", message="a"))
        since = forge_alerts.current()[0]["since"]
        self.assertTrue(raise_("T", kind=forge_alerts.GITEA_TOKEN_REFUSED, forge="g", message="b"))
        (alert,) = forge_alerts.current()
        self.assertEqual(("b", since), (alert["message"], alert["since"]))  # first seen kept
        self.assertTrue(forge_alerts.clear("T"))
        self.assertFalse(forge_alerts.clear("T"))
        self.assertEqual([], forge_alerts.current())

    def test_unknown_kinds_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            forge_alerts.raise_alert("T", kind="other", forge="g", message="m")

    def test_host_stats_carries_them(self) -> None:
        forge_alerts.raise_alert("T", kind=forge_alerts.GITHUB_REAUTHORIZE, forge="github.com",
                                 message="reconnect")
        with mock.patch.object(host_stats, "collect", return_value={}), \
                mock.patch.object(host_stats.budget, "host_budget"), \
                mock.patch.object(host_stats.budget, "block", return_value={}), \
                mock.patch.object(host_stats, "about", return_value={}):
            snap = host_stats.snapshot(None)
        self.assertEqual(["T"], [a["key"] for a in snap["forge_alerts"]])  # type: ignore[union-attr]


class TestRenewerAlerts(unittest.TestCase):
    def setUp(self) -> None:
        forge_alerts.reset()
        self.addCleanup(forge_alerts.reset)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))
        provision.save_upstreams("s1", (HTTPS,))
        self.enterContext(mock.patch(
            "bot_bottle.control.hostctl.github_git_tokens.adoption.read",
            return_value=SimpleNamespace(registry_id="r1")))
        self.bottles = SimpleNamespace(sessions=SimpleNamespace(
            records=lambda: [SimpleNamespace(id="s1")]))

    def _renewer(self, answers: list[Any]) -> GitTokenRenewer:
        it = iter(answers)

        def token_for(_secret: str) -> str | None:
            a = next(it)
            if isinstance(a, Exception):
                raise a
            return a
        transport = _Transport()
        return GitTokenRenewer(self.bottles, token_for=token_for,
                               transport_for=lambda i: transport, expires_for=lambda s: None)

    def test_a_dead_authorization_alerts_once_and_clears_on_success(self) -> None:
        dead = ReauthorizeError("GitHub refused to renew the authorization")
        renewer = self._renewer([dead, dead, "fresh"])
        with mock.patch("bot_bottle.control.hostctl.github_git_tokens.log.warn") as warned:
            renewer.renew_all()
            renewer.renew_all()
        warned.assert_called_once()  # not every pass
        (alert,) = forge_alerts.current()
        self.assertEqual(("GITHUB_COM_TOKEN", forge_alerts.GITHUB_REAUTHORIZE, "github.com"),
                         (alert["key"], alert["kind"], alert["forge"]))
        self.assertEqual(["s1"], renewer.renew_all())
        self.assertEqual([], forge_alerts.current())

    def test_network_trouble_is_only_a_warning(self) -> None:
        from bot_bottle.contrib.github.forge_api import ForgeApiError  # pylint: disable=import-outside-toplevel
        renewer = self._renewer([ForgeApiError("couldn't reach GitHub")])
        with mock.patch("bot_bottle.control.hostctl.github_git_tokens.log.warn") as warned:
            renewer.renew_all()
        warned.assert_called_once()
        self.assertEqual([], forge_alerts.current())


class TestGiteaRefusal(unittest.TestCase):
    def setUp(self) -> None:
        forge_alerts.reset()
        self.addCleanup(forge_alerts.reset)
        self.prov = GiteaDeployKeyProvisioner(token="t", api_url="https://gitea.example")

    def test_401_and_403_are_refusals_with_the_fix(self) -> None:
        for code in (401, 403):
            for call in (lambda: self.prov.create("o/r", "title"),
                         lambda: self.prov.delete("o/r", "7")):
                with self.subTest(code=code), \
                        mock.patch("urllib.request.urlopen", side_effect=_http(code)), \
                        mock.patch("bot_bottle.contrib.gitea.deploy_key_provisioner."
                                   "generate_keypair", return_value=(b"k", "ssh-ed25519 K")), \
                        self.assertRaises(ForgeTokenRefusedError) as ctx:
                    call()
                self.assertIn("bot-bottle forge connect https://gitea.example", str(ctx.exception))

    def test_other_errors_are_not(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http(500)), \
                self.assertRaises(RuntimeError) as ctx:
            self.prov.delete("o/r", "7")
        self.assertNotIsInstance(ctx.exception, ForgeTokenRefusedError)

    def test_a_refused_gitea_token_alerts_until_it_works(self) -> None:
        alert = provision._token_alert  # pylint: disable=protected-access
        with self.assertRaises(ForgeTokenRefusedError):
            with alert("gitea", "GITEA_TOKEN", "gitea.example"):
                raise ForgeTokenRefusedError("refused")
        (a,) = forge_alerts.current()
        self.assertEqual((forge_alerts.GITEA_TOKEN_REFUSED, "gitea.example"),
                         (a["kind"], a["forge"]))
        with alert("gitea", "GITEA_TOKEN", "gitea.example"):
            pass
        self.assertEqual([], forge_alerts.current())

    def test_github_deploy_keys_leave_alerts_to_the_renewer(self) -> None:
        with self.assertRaises(ForgeTokenRefusedError):
            with provision._token_alert("github", "GITHUB_COM_TOKEN", "github.com"):  # pylint: disable=protected-access
                raise ForgeTokenRefusedError("refused")
        self.assertEqual([], forge_alerts.current())


class _Transport:
    def __init__(self) -> None:
        self.execs: list[list[str]] = []

    def exec(self, argv: list[str]) -> None:
        self.execs.append(argv)

    def cp_into(self, src: str, dest: str) -> None:
        del src, dest


class TestTokenExpiryInTheGateway(unittest.TestCase):
    def test_the_expiry_goes_beside_the_token(self) -> None:
        t = _Transport()
        provision_gateway.push_tokens(t, "b1", (HTTPS,), token_for=lambda s: "tok",
                                      expires_for=lambda s: 1790000000)
        self.assertIn(["sh", "-c", "printf %s 1790000000 > "
                                   "/dev/shm/git-gate/b1/GITHUB_COM_TOKEN-token.expires"], t.execs)

    def test_no_expiry_removes_a_stale_one(self) -> None:
        t = _Transport()
        provision_gateway.push_tokens(t, "b1", (HTTPS,), token_for=lambda s: "tok",
                                      expires_for=lambda s: None)
        self.assertIn(["sh", "-c", "rm -f /dev/shm/git-gate/b1/GITHUB_COM_TOKEN-token.expires"],
                      t.execs)

    def test_it_is_read_from_the_host_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "secrets"
            path.write_text("GITHUB_COM_TOKEN_EXPIRES=1790000000\nOTHER_EXPIRES=junk\n",
                            encoding="utf-8")
            path.chmod(0o600)
            with mock.patch("bot_bottle.paths.host_secrets_path", return_value=path):
                self.assertEqual(1790000000,
                                 provision_gateway.github_token_expires("GITHUB_COM_TOKEN"))
                self.assertIsNone(provision_gateway.github_token_expires("OTHER"))
                self.assertIsNone(provision_gateway.github_token_expires("MISSING"))


@unittest.skipUnless(shutil.which("sh"), "needs sh")
class TestExpiredOrRevoked(unittest.TestCase):
    """The pre-receive hook's `upstream_hint`, run as it is in the hook."""

    def _hint(self, expires: str | None) -> str:
        hook = git_gate_render_hook()
        start = hook.index("upstream_hint() {")
        func = hook[start:hook.index("\n}\n", start) + 3]
        with tempfile.TemporaryDirectory() as d:
            token_file = Path(d) / "tok"
            token_file.write_text("t", encoding="utf-8")
            if expires is not None:
                Path(f"{token_file}.expires").write_text(expires, encoding="utf-8")
            err = Path(d) / "err"
            err.write_text("remote: Invalid username or token.\n", encoding="utf-8")
            script = (f"transport=https\ntoken_file={token_file}\n{func}\n"
                      f"upstream_hint {err}\n")
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                               check=False, env={**os.environ})
        return r.stderr

    def test_an_expired_token_points_at_the_host_controller(self) -> None:
        msg = self._hint(str(int(time.time()) - 60))
        self.assertIn("expired", msg)
        self.assertIn("bot-bottle up", msg)
        self.assertNotIn("--reauthorize", msg)

    def test_an_unexpired_refusal_means_revoked(self) -> None:
        msg = self._hint(str(int(time.time()) + 3600))
        self.assertIn("revoked", msg)
        self.assertIn("--reauthorize", msg)

    def test_without_an_expiry_it_names_both(self) -> None:
        msg = self._hint(None)
        self.assertIn("bot-bottle up", msg)
        self.assertIn("--reauthorize", msg)


if __name__ == "__main__":
    unittest.main()
