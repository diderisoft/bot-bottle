"""github.com through the bot-bottle GitHub App's user access tokens (ADR
0012): the device flow, renewal, reachability, connecting, the host's link
requests and cards, and the CLI."""
# pylint: disable=protected-access,attribute-defined-outside-init,unbalanced-tuple-unpacking
# pylint: disable=consider-using-with

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle import github_connect
from bot_bottle.cli.commands import forge as forge_cmd
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.contrib.github import user_token
from bot_bottle.contrib.github.user_token import (
    AppConfig, DeviceCode, ReauthorizeError, TokenSet,
)
from bot_bottle.control.hostctl import remote, remote_actions, repo_grants
from bot_bottle.forge_connect import ConnectError, ForgeEntry, write_forge
from bot_bottle.forge_kinds import ForgeApiError
from bot_bottle.manifest.forges import forges_raw, resolve_forge
from bot_bottle.manifest.util import ManifestError
from bot_bottle.secret_source import FileSecrets, MappingSecrets, remove_secret, write_secret

CONFIG = AppConfig(slug="bot-bottle-dev", client_id="Iv1.abc")


class _Http:
    """Fake GitHub: a queue of answers per (method, path)."""

    def __init__(self, answers: dict[tuple[str, str], list[tuple[int, object]]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict[str, str], object]] = []

    def __call__(self, req: urllib.request.Request, timeout: float = 0) -> Any:
        url = req.full_url.split("://", 1)[1]
        path = "/" + url.split("/", 1)[1]
        method = req.get_method()
        body: object = None
        data = req.data
        if isinstance(data, bytes):
            body = dict(urllib.parse.parse_qsl(data.decode()))
        self.calls.append((method, path, dict(req.header_items()), body))
        queue = self.answers.get((method, path)) or [(404, {"message": "Not Found"})]
        status, answer = queue.pop(0) if len(queue) > 1 else queue[0]
        raw = json.dumps(answer).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {},  # type: ignore[arg-type]
                                         io.BytesIO(raw))
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = raw
        return resp


class _Base(unittest.TestCase):
    def http(self, answers: dict[tuple[str, str], list[tuple[int, object]]]) -> _Http:
        fake = _Http(answers)
        patcher = mock.patch.object(user_token.urllib.request, "urlopen", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


DEVICE = {"device_code": "dc", "user_code": "ABCD-1234",
          "verification_uri": "https://github.com/login/device", "expires_in": 900, "interval": 5}


class TestConfig(unittest.TestCase):
    def test_defaults_and_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(AppConfig("bot-bottle", ""), user_token.app_config(root))
            user_token.save_app_config(CONFIG, root)
            self.assertEqual(CONFIG, user_token.app_config(root))
            (root / "github-app.json").write_text("[]")
            self.assertEqual("bot-bottle", user_token.app_config(root).slug)
        self.assertEqual("https://github.com/apps/bot-bottle-dev/installations/new",
                         CONFIG.install_url)


class TestDeviceFlow(_Base):
    def test_start_needs_a_client_id_and_device_flow(self) -> None:
        with self.assertRaises(ForgeApiError):
            user_token.start_device("")
        http = self.http({("POST", "/login/device/code"): [(200, DEVICE)]})
        device = user_token.start_device("Iv1.abc")
        self.assertEqual(("ABCD-1234", 900, 5), (device.user_code, device.expires_in,
                                                device.interval))
        self.assertEqual({"client_id": "Iv1.abc"}, http.calls[0][3])
        self.http({("POST", "/login/device/code"): [(200, {"error": "device_flow_disabled"})]})
        with self.assertRaises(ForgeApiError) as cm:
            user_token.start_device("Iv1.abc")
        self.assertIn("device flow enabled", str(cm.exception))
        self.http({("POST", "/login/device/code"): [(200, {"user_code": "x"})]})
        with self.assertRaises(ForgeApiError):
            user_token.start_device("Iv1.abc")

    def poll(self, answers: list[tuple[int, object]], **kw: Any) -> TokenSet:
        http = self.http({("POST", "/login/oauth/access_token"): answers})
        sleeps: list[float] = []
        self.addCleanup(lambda: None)
        result = user_token.wait_for_token(
            "Iv1.abc", DeviceCode("dc", "U", "v", 900, 5), sleep=sleeps.append,
            clock=lambda: 1000.0, **kw)
        self.sleeps, self.calls = sleeps, http.calls
        return result

    def test_pending_slow_down_then_a_token(self) -> None:
        tokens = self.poll([(200, {"error": "authorization_pending"}),
                            (200, {"error": "slow_down"}),
                            (200, {"access_token": "ghu_1", "refresh_token": "ghr_1",
                                   "expires_in": 28800})])
        self.assertEqual(TokenSet("ghu_1", "ghr_1", 1000 + 28800), tokens)
        self.assertEqual([5, 5, 10], self.sleeps)
        self.assertEqual({"client_id": "Iv1.abc", "device_code": "dc",
                          "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                         self.calls[0][3])
        self.assertNotIn("client_secret", self.calls[0][3])  # type: ignore[operator]

    def test_a_non_expiring_token(self) -> None:
        self.assertEqual(TokenSet("ghu_1"), self.poll([(200, {"access_token": "ghu_1"})]))

    def test_denied_expired_other_and_cancelled(self) -> None:
        for error, says in (("access_denied", "declined"), ("expired_token", "expired"),
                            ("incorrect_client_credentials", "incorrect_client")):
            with self.subTest(error=error):
                with self.assertRaises(ForgeApiError) as cm:
                    self.poll([(200, {"error": error})])
                self.assertIn(says, str(cm.exception))
        with self.assertRaises(ForgeApiError):
            self.poll([(200, {"error": "authorization_pending"})], cancelled=lambda: True)
        with self.assertRaises(ForgeApiError):
            self.poll([(200, {})])

    def test_http_errors_and_odd_bodies(self) -> None:
        self.http({("POST", "/login/device/code"): [(500, {})]})
        with self.assertRaises(ForgeApiError):
            user_token.start_device("c")
        with mock.patch.object(user_token.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            with self.assertRaises(ForgeApiError):
                user_token.start_device("c")
        self.http({("POST", "/login/device/code"): [(200, [1])]})
        with self.assertRaises(ForgeApiError):
            user_token.start_device("c")


class TestRenewal(_Base):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "secrets"

    def test_store_keeps_companions_only_for_expiring_tokens(self) -> None:
        user_token.store(self.path, "GH_TOKEN", TokenSet("a", "r", 99), "cid")
        self.assertEqual(("a", "r", "99", "cid"), tuple(
            FileSecrets(self.path).get(n) for n in
            ("GH_TOKEN", "GH_TOKEN_REFRESH", "GH_TOKEN_EXPIRES", "GH_TOKEN_CLIENT_ID")))
        user_token.store(self.path, "GH_TOKEN", TokenSet("b"), "cid")
        secrets = FileSecrets(self.path)
        self.assertEqual(("b", None), (secrets.get("GH_TOKEN"), secrets.get("GH_TOKEN_REFRESH")))

    def test_a_fresh_or_non_expiring_token_is_used_as_is(self) -> None:
        self.assertEqual("a", user_token.current_token("T", MappingSecrets({"T": "a"})))
        self.assertIsNone(user_token.current_token("T", MappingSecrets({})))
        fresh = MappingSecrets({"T": "a", "T_EXPIRES": str(int(time.time()) + 3600)})
        self.assertEqual("a", user_token.current_token("T", fresh))

    def test_an_expiring_token_is_renewed_without_a_secret(self) -> None:
        user_token.store(self.path, "T", TokenSet("old", "ghr_old", 1000), "cid")
        http = self.http({("POST", "/login/oauth/access_token"): [
            (200, {"access_token": "new", "refresh_token": "ghr_new", "expires_in": 28800})]})
        token = user_token.current_token("T", FileSecrets(self.path), path=self.path, now=900)
        self.assertEqual("new", token)
        self.assertEqual({"client_id": "cid", "grant_type": "refresh_token",
                          "refresh_token": "ghr_old"}, http.calls[0][3])
        stored = FileSecrets(self.path)
        self.assertEqual(("new", "ghr_new", str(900 + 28800)),
                         (stored.get("T"), stored.get("T_REFRESH"), stored.get("T_EXPIRES")))

    def test_renewed_meanwhile_by_another_launch(self) -> None:
        user_token.store(self.path, "T", TokenSet("old", "r", 1000), "cid")
        stale = FileSecrets(self.path)
        stale.get("T")
        user_token.store(self.path, "T", TokenSet("theirs", "r2", 99999), "cid")
        self.http({})
        self.assertEqual("theirs", user_token.current_token("T", stale, path=self.path, now=900))

    def test_renewal_refused_or_impossible_means_reauthorize(self) -> None:
        user_token.store(self.path, "T", TokenSet("old", "r", 1000), "cid")
        self.http({("POST", "/login/oauth/access_token"): [(200, {"error": "bad_refresh_token"})]})
        with self.assertRaises(ReauthorizeError) as cm:
            user_token.current_token("T", FileSecrets(self.path), path=self.path, now=900)
        self.assertIn("connect github", str(cm.exception).lower())
        remove_secret(self.path, "T_REFRESH")
        with self.assertRaises(ReauthorizeError):
            user_token.current_token("T", FileSecrets(self.path), path=self.path, now=900)

    def test_provisioning_uses_the_renewed_token(self) -> None:
        from bot_bottle.git_gate import provision
        with mock.patch("bot_bottle.contrib.github.user_token.current_token",
                        return_value="fresh") as current:
            self.assertEqual("fresh", provision._forge_token("github", "T", MappingSecrets({})))
        current.assert_called_once()
        self.assertEqual("g", provision._forge_token("gitea", "T", MappingSecrets({"T": "g"})))


class TestReach(_Base):
    def test_login_installations_reachable(self) -> None:
        self.http({
            ("GET", "/user"): [(200, {"login": "didericis"})],
            ("GET", "/user/installations?per_page=100"): [(200, {"installations": [
                {"account": {"login": "didericis"}, "repository_selection": "all"},
                {"account": None}, "junk"]})],
            ("GET", "/repos/acme/app"): [(200, {"id": 1})],
            ("GET", "/repos/acme/hidden"): [(404, {})],
            ("GET", "/repos/acme/gone"): [(401, {})],
            ("GET", "/repos/acme/boom"): [(500, {})],
        })
        self.assertEqual("didericis", user_token.login("t"))
        self.assertEqual([{"account": "didericis", "repository_selection": "all"},
                          {"account": "", "repository_selection": ""}],
                         user_token.installations("t"))
        self.assertTrue(user_token.reachable("t", "acme/app"))
        self.assertFalse(user_token.reachable("t", "acme/hidden"))
        with self.assertRaises(ReauthorizeError):
            user_token.reachable("t", "acme/gone")
        with self.assertRaises(ForgeApiError):
            user_token.reachable("t", "acme/boom")

    def test_errors(self) -> None:
        self.http({("GET", "/user"): [(200, {})],
                   ("GET", "/user/installations?per_page=100"): [(401, {})]})
        with self.assertRaises(ForgeApiError):
            user_token.login("t")
        with self.assertRaises(ReauthorizeError):
            user_token.installations("t")
        self.http({("GET", "/user"): [(500, {})],
                   ("GET", "/user/installations?per_page=100"): [(500, {})]})
        with self.assertRaises(ForgeApiError):
            user_token.login("t")
        with self.assertRaises(ForgeApiError):
            user_token.installations("t")
        with mock.patch.object(user_token.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            for call in (lambda: user_token.login("t"), lambda: user_token.installations("t"),
                         lambda: user_token.reachable("t", "a/b")):
                with self.assertRaises(ForgeApiError):
                    call()


class TestManifest(unittest.TestCase):
    def test_app_is_kept_on_github_forges_only(self) -> None:
        forge = {"kind": "github", "url": "https://github.com", "token": "$gh", "app": "bot-bottle"}
        raw = forges_raw("b", {"git-gate": {"forges": {"github.com": forge}}})
        resolved = resolve_forge("github.com", raw["github.com"],
                                 {"gh": {"source": "secret", "secret": "GITHUB_COM_TOKEN"}}, "b")
        self.assertEqual(("github", "bot-bottle"), (resolved.kind, resolved.app))
        with self.assertRaises(ManifestError):
            forges_raw("b", {"git-gate": {"forges": {"g.example": {
                "kind": "gitea", "url": "https://g.example", "token": "$t", "app": "x"}}}})


class TestConnect(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bottles = self.root / "bottles"
        self.secrets = self.root / "secrets"
        self.enterContext(mock.patch("bot_bottle.paths.host_secrets_path",
                                     return_value=self.secrets))
        self.enterContext(mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                                     return_value="ssh-ed25519 AAAAgh"))
        self.enterContext(mock.patch("bot_bottle.manifest.versions.record_or_warn"))
        self.enterContext(mock.patch("bot_bottle.contrib.github.user_token.login",
                                     return_value="didericis"))

    def test_finish_writes_the_forge_and_keeps_the_token(self) -> None:
        done = github_connect.finish(TokenSet("ghu_1", "ghr_1", 5), CONFIG, cwd=self.root,
                                     host_bottles=self.bottles)
        self.assertEqual(("github.com", "github", "didericis", "bot-bottle-dev", True,
                          "GITHUB_COM_TOKEN"),
                         (done.key, done.bottle, done.login, done.app, done.expiring,
                          done.secret_name))
        fm, _ = parse_frontmatter((self.bottles / "github.md").read_text())
        forge = fm["git-gate"]["forges"]["github.com"]  # type: ignore[index]
        self.assertEqual(("github", "bot-bottle-dev", "didericis", "ssh-ed25519 AAAAgh"),
                         (forge["kind"], forge["app"], forge["account"], forge["host_key"]))
        secrets = FileSecrets(self.secrets)
        self.assertEqual(("ghu_1", "ghr_1", "Iv1.abc"),
                         (secrets.get("GITHUB_COM_TOKEN"), secrets.get("GITHUB_COM_TOKEN_REFRESH"),
                          secrets.get("GITHUB_COM_TOKEN_CLIENT_ID")))

    def test_finish_refusals(self) -> None:
        with self.assertRaises(ConnectError):
            github_connect.finish(TokenSet("t"), CONFIG, bottle="./p", cwd=self.root,
                                  host_bottles=self.bottles)
        with mock.patch("bot_bottle.contrib.github.user_token.login",
                        side_effect=ForgeApiError("401")):
            with self.assertRaises(ConnectError):
                github_connect.finish(TokenSet("t"), CONFIG, cwd=self.root,
                                      host_bottles=self.bottles)
        with mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                        side_effect=RuntimeError("timeout")):
            with self.assertRaises(ConnectError) as cm:
                github_connect.finish(TokenSet("t"), CONFIG, cwd=self.root,
                                      host_bottles=self.bottles)
        self.assertIn("over SSH", str(cm.exception))

    def test_authorize_runs_the_whole_flow(self) -> None:
        shown: list[Any] = []
        device = DeviceCode("dc", "U-1", "v", 900, 5)
        with mock.patch("bot_bottle.contrib.github.user_token.start_device",
                        return_value=device), \
                mock.patch("bot_bottle.contrib.github.user_token.wait_for_token",
                           return_value=TokenSet("t")):
            done = github_connect.authorize(CONFIG, on_code=shown.append, cwd=self.root,
                                            host_bottles=self.bottles)
        self.assertEqual(([device], False), (shown, done.expiring))
        with mock.patch("bot_bottle.contrib.github.user_token.start_device",
                        side_effect=ForgeApiError("no client id")):
            with self.assertRaises(ConnectError):
                github_connect.authorize(CONFIG, on_code=shown.append, cwd=self.root,
                                         host_bottles=self.bottles)

    def wait_state(self, want: str) -> dict[str, object]:
        for _ in range(200):
            flow = github_connect.status(self.bottles, CONFIG)["flow"]
            if flow["state"] == want:  # type: ignore[index]
                return flow  # type: ignore[return-value]
            time.sleep(0.01)
        self.fail(f"never reached {want}")

    def test_background_flow_connects_and_reports(self) -> None:
        release = threading.Event()

        def wait(*_: Any, **__: Any) -> TokenSet:
            release.wait(5)
            return TokenSet("ghu_bg")

        with mock.patch("bot_bottle.contrib.github.user_token.start_device",
                        return_value=DeviceCode("dc", "U-2", "https://github.com/login/device",
                                                900, 5)), \
                mock.patch("bot_bottle.contrib.github.user_token.wait_for_token", wait), \
                mock.patch("bot_bottle.contrib.github.user_token.installations",
                           return_value=[{"account": "didericis",
                                          "repository_selection": "all"}]):
            started = github_connect.start_background(cwd=self.root, host_bottles=self.bottles,
                                                      config=CONFIG)
            self.assertEqual({"user_code": "U-2", "verification_uri":
                              "https://github.com/login/device", "expires_in": 900}, started)
            self.assertEqual("U-2", self.wait_state("waiting")["user_code"])
            release.set()
            self.assertEqual("didericis", self.wait_state("connected")["login"])
            status = github_connect.status(self.bottles, CONFIG)
        self.assertEqual(("bot-bottle-dev", True), (status["app"], status["client_id_set"]))
        (connected,) = status["connected"]  # type: ignore[misc]
        self.assertEqual([{"account": "didericis", "repository_selection": "all"}],
                         connected["installations"])
        self.assertNotIn("ghu_bg", json.dumps(status))

    def test_background_flow_errors(self) -> None:
        with mock.patch("bot_bottle.contrib.github.user_token.start_device",
                        side_effect=ForgeApiError("no device flow")):
            with self.assertRaises(ConnectError):
                github_connect.start_background(cwd=self.root, host_bottles=self.bottles,
                                                config=CONFIG)
        for failure in (ForgeApiError("declined"), ValueError("x")):
            with self.subTest(failure=failure), \
                    mock.patch("bot_bottle.contrib.github.user_token.start_device",
                               return_value=DeviceCode("dc", "U", "v", 900, 5)), \
                    mock.patch("bot_bottle.contrib.github.user_token.wait_for_token",
                               side_effect=failure):
                github_connect.start_background(cwd=self.root, host_bottles=self.bottles,
                                                config=CONFIG)
                self.assertIn("error", self.wait_state("error"))

    def test_status_problems(self) -> None:
        write_forge(self.bottles / "github.md", ForgeEntry(
            key="github.com", kind="github", url="https://github.com",
            ssh="ssh://git@github.com", account="me", host_key="", app="bot-bottle-dev"))
        write_forge(self.bottles / "pat.md", ForgeEntry(
            key="github.com", kind="github", url="https://github.com",
            ssh="ssh://git@github.com", account="me", host_key=""))
        (self.bottles / "broken.md").write_text("---\nx: [\n---\n")
        (info,) = github_connect.status(self.bottles, CONFIG)["connected"]  # type: ignore[misc]
        self.assertIn("isn't in the host secrets", info["error"])
        write_secret(self.secrets, "GITHUB_COM_TOKEN", "t")
        with mock.patch("bot_bottle.contrib.github.user_token.installations",
                        side_effect=ReauthorizeError("re-authorize")):
            (info,) = github_connect.status(self.bottles, CONFIG)["connected"]  # type: ignore[misc]
        self.assertEqual("re-authorize", info["error"])


class TestLinkRequests(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        self.link = remote.ConsoleLink(mock.Mock(), send, agents=lambda: [])
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def handle(self, **msg: Any) -> dict[str, Any]:
        asyncio.run(self.link.handle({"request_id": "q1", **msg}))
        return self.sent[-1]

    def test_authorize_start(self) -> None:
        with mock.patch("bot_bottle.github_connect.start_background",
                        return_value={"user_code": "U", "verification_uri": "v",
                                      "expires_in": 900}):
            reply = self.handle(type="github_authorize_start")
        self.assertEqual(("github_authorize", "q1", "U"),
                         (reply["type"], reply["request_id"], reply["user_code"]))
        with mock.patch("bot_bottle.github_connect.start_background",
                        side_effect=ConnectError("no client id")):
            self.assertEqual("no client id", self.handle(type="github_authorize_start")["error"])
        with mock.patch("bot_bottle.github_connect.start_background",
                        side_effect=ValueError("ghu_secret")):
            self.assertNotIn("ghu_secret", self.handle(type="github_authorize_start")["error"])

    def test_status(self) -> None:
        with mock.patch("bot_bottle.github_connect.status", return_value={"app": "bot-bottle"}):
            self.assertEqual("bot-bottle", self.handle(type="github_status")["app"])
        with mock.patch("bot_bottle.github_connect.status", side_effect=OSError("x")):
            self.assertIn("error", self.handle(type="github_status"))


def _ask(tool: str, proposed: str) -> SimpleNamespace:
    return SimpleNamespace(id="p1", bottle="b1", session="s", tool=tool, proposed=proposed,
                           justification="", arrived="2026-11-02T00:00:00+00:00",
                           orchestrator="http://orch")


APP_FORGE = SimpleNamespace(kind="github", app="bot-bottle-dev", token_secret="GITHUB_COM_TOKEN",
                            url="https://github.com")


class TestCards(unittest.TestCase):
    def test_forge_connect_for_github(self) -> None:
        with mock.patch.object(remote_actions, "_connected_forges", return_value={}), \
                mock.patch("bot_bottle.contrib.github.user_token.app_config",
                           return_value=CONFIG):
            lines, options = remote_actions.forge_attention(
                _ask("forge-connect", "https://github.com"), "github")
        self.assertEqual(["github_authorize"], options)
        self.assertIn("install: https://github.com/apps/bot-bottle-dev/installations/new", lines)
        self.assertFalse(any(line.startswith("token") for line in lines))
        connected = mock.Mock(return_value={"github.com": "github"})
        with mock.patch.object(remote_actions, "_connected_forges", connected):
            refusal, _ = remote_actions.vet(_ask("forge-connect", "https://github.com"))
        self.assertIn("already connected to this host", refusal)

    def reach(self, *, token: str | None = "t", reachable: object = True) -> Any:
        fake = mock.Mock()
        if isinstance(reachable, Exception):
            fake.side_effect = reachable
        else:
            fake.return_value = reachable
        with mock.patch("bot_bottle.contrib.github.user_token.current_token",
                        return_value=token), \
                mock.patch("bot_bottle.contrib.github.user_token.reachable", fake):
            return remote_actions._app_reach_lines(APP_FORGE, "github.com/acme/app")

    def test_reach_lines(self) -> None:
        self.assertEqual(([], False), self.reach())
        lines, again = self.reach(reachable=False)
        self.assertEqual("install: https://github.com/apps/bot-bottle-dev/installations/new",
                         lines[0])
        self.assertIn("install it on acme", lines[1])
        self.assertFalse(again)
        self.assertTrue(self.reach(token=None)[1])
        self.assertTrue(self.reach(reachable=ReauthorizeError("gone"))[1])
        self.assertEqual(([], False), self.reach(reachable=ForgeApiError("down")))
        self.assertEqual(([], False), remote_actions._app_reach_lines(
            SimpleNamespace(kind="github", app=""), "github.com/a/b"))

    def test_add_repo_card(self) -> None:
        found = SimpleNamespace(forge=APP_FORGE, bottle="")
        with mock.patch.object(repo_grants, "find_forge", return_value=found), \
                mock.patch("bot_bottle.approvals.keep_target", return_value=None), \
                mock.patch.object(remote_actions, "_app_reach_lines",
                                  return_value=(["reauthorize: x"], True)):
            lines, options = remote_actions.repo_attention(_ask("add-repo",
                                                                "github.com/a/b:write"))
        self.assertIn("reauthorize: x", lines)
        self.assertEqual(["github_authorize", "add_repo", "add_repo_write"], options)


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.web = self.enterContext(mock.patch.object(forge_cmd.webbrowser, "open"))
        self.done = github_connect.GitHubConnected(
            key="github.com", bottle="github", path=Path("/x/github.md"), login="me",
            app="bot-bottle", install_url="i", secret_name="GITHUB_COM_TOKEN", expiring=True)

    def test_github_is_the_device_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("bot_bottle.paths.bot_bottle_root", return_value=Path(tmp)), \
                mock.patch("bot_bottle.github_connect.authorize", return_value=self.done) as auth:
            self.assertEqual(0, forge_cmd.cmd_forge(
                ["connect", "github", "--app", "bot-bottle-dev", "--client-id", "Iv1.x"]))
            self.assertEqual(AppConfig("bot-bottle-dev", "Iv1.x"),
                             user_token.app_config(Path(tmp)))
        config = auth.call_args.args[0]
        self.assertEqual("Iv1.x", config.client_id)
        auth.call_args.kwargs["on_code"](DeviceCode("dc", "U", "https://github.com/login/device",
                                                    900, 5))
        self.web.assert_called_once_with("https://github.com/login/device")

    def test_errors_and_the_token_fallback(self) -> None:
        with mock.patch("bot_bottle.github_connect.authorize",
                        side_effect=ConnectError("declined")):
            with self.assertRaises(SystemExit):
                forge_cmd.cmd_forge(["connect", "github", "--no-browser"])
        with self.assertRaises(SystemExit):
            forge_cmd.cmd_forge(["connect", "github", "a", "b"])
        with self.assertRaises(SystemExit):
            forge_cmd.cmd_forge(["connect", "github", "--client-id"])
        with mock.patch.object(forge_cmd, "detect", return_value="github"), \
                mock.patch.object(forge_cmd, "_read_token", return_value="pat") as read, \
                mock.patch.object(forge_cmd, "connect_forge",
                                  side_effect=ConnectError("bad token")):
            with self.assertRaises(SystemExit):
                forge_cmd.cmd_forge(["connect", "github", "--token", "--no-browser"])
        self.assertEqual("https://github.com", read.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
