"""Forge kinds (Gitea, GitHub): recognizing a forge, GitHub's API and deploy
keys, `forge connect` for GitHub, and refusing an agent's forge or repo
request no card could meet."""

from __future__ import annotations

import asyncio
import contextlib
import email.message
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot_bottle import forge_kinds
from bot_bottle.cli.commands.forge import cmd_forge
from bot_bottle.contract import forges as contract_forges
from bot_bottle.contrib.github import forge_api as github
from bot_bottle.contrib.github.deploy_key_provisioner import GitHubDeployKeyProvisioner
from bot_bottle.control.client import AlreadyAnswered
from bot_bottle.control.git_gate_render import git_gate_upstreams_for_bottle
from bot_bottle.control.hostctl import remote_actions, repo_grants
from bot_bottle.deploy_key_provisioner import DeployKeyCollisionError, get_provisioner
from bot_bottle.forge_connect import ConnectError, connect
from bot_bottle.forge_connections import connections
from bot_bottle.git_gate.provision import DeployKeyRef, deploy_key_refs, revoke_key
from bot_bottle.lib.log import Die
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from tests.unit import use_bottle_root
from tests.unit._forges import GITHUB, connected


def _response(body: object) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    return resp


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "err", email.message.Message(),
                                  io.BytesIO(body.encode()))


class TestContract(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual("Gitea, GitHub", contract_forges.supported())
        self.assertTrue(contract_forges.known_unsupported("WWW.GitLab.com"))
        self.assertFalse(contract_forges.known_unsupported("gitea.example.com"))
        self.assertEqual("gitlab.com isn't a forge bot-bottle can connect; supported: "
                         "Gitea, GitHub", contract_forges.unsupported_forge("gitlab.com"))


class TestDetect(unittest.TestCase):
    def test_github_com_without_asking(self) -> None:
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual("github", forge_kinds.detect("https://github.com"))
            self.assertEqual("github", forge_kinds.detect("https://www.github.com/x"))
            self.assertEqual("", forge_kinds.detect("https://gitlab.com"))
            self.assertEqual("", forge_kinds.detect("not a url"))
        urlopen.assert_not_called()

    def test_asks_another_host(self) -> None:
        with mock.patch("urllib.request.urlopen",
                        return_value=_response({"version": "1.22"})) as urlopen:
            self.assertEqual("gitea", forge_kinds.detect("https://git.example.com"))
        self.assertEqual("https://git.example.com/api/v1/version",
                         urlopen.call_args.args[0].full_url)
        self.assertEqual(forge_kinds.DETECT_TIMEOUT_SECS, urlopen.call_args.kwargs["timeout"])
        server = [urllib.error.URLError("no gitea"), _response({"installed_version": "3.14"})]
        with mock.patch("urllib.request.urlopen", side_effect=server) as urlopen:
            self.assertEqual("github", forge_kinds.detect("https://ghe.example.com"))
        self.assertEqual("https://ghe.example.com/api/v3/meta",
                         urlopen.call_args.args[0].full_url)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertEqual("", forge_kinds.detect("https://nowhere.example.com"))

    def test_forge_api_and_label(self) -> None:
        self.assertIs(github, forge_kinds.forge_api("github"))
        with self.assertRaises(ValueError):
            forge_kinds.forge_api("gitlab")
        self.assertEqual("GitHub", forge_kinds.label("github"))
        self.assertEqual("other", forge_kinds.label("other"))


class TestGitHubApi(unittest.TestCase):
    def test_urls(self) -> None:
        self.assertEqual("https://api.github.com", github.api_base("https://github.com/"))
        self.assertEqual("https://ghe.example.com/api/v3",
                         github.api_base("https://ghe.example.com/"))
        self.assertEqual("https://github.com/settings/personal-access-tokens/new",
                         github.token_page("https://www.github.com"))
        self.assertEqual("https://ghe.example.com/settings/personal-access-tokens/new",
                         github.token_page("https://ghe.example.com/"))
        self.assertIn("Administration: read & write", github.TOKEN_HELP)

    def test_check_token_pages_and_counts_admin(self) -> None:
        page1 = [{"permissions": {"admin": i == 0}} for i in range(100)]
        page2 = [{"permissions": {"admin": True}}, {"permissions": "?"}, "junk"]
        replies = [_response({"login": "octo"}), _response(page1), _response(page2)]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            check = github.check_token("https://github.com", "tok")
        self.assertEqual(forge_kinds.TokenCheck("octo", "ssh://git@github.com", 2, 103), check)
        first = urlopen.call_args_list[0].args[0]
        self.assertEqual("https://api.github.com/user", first.full_url)
        self.assertEqual("Bearer tok", first.get_header("Authorization"))
        self.assertEqual("bot-bottle", first.get_header("User-agent"))
        self.assertEqual("https://api.github.com/user/repos?per_page=100&page=2",
                         urlopen.call_args_list[2].args[0].full_url)

    def test_check_token_on_a_server_finds_its_ssh(self) -> None:
        replies = [_response({"login": "octo"}),
                   _response([{"ssh_url": "git@ghe.example.com:o/r.git"}])]
        with mock.patch("urllib.request.urlopen", side_effect=replies):
            check = github.check_token("https://ghe.example.com", "tok")
        self.assertEqual("ssh://git@ghe.example.com", check.ssh)

    def test_errors(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401, "Bad credentials")):
            with self.assertRaisesRegex(forge_kinds.ForgeApiError, "401.*Bad credentials"):
                github.check_token("https://github.com", "tok")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("reset")):
            with self.assertRaisesRegex(forge_kinds.ForgeApiError, "couldn't reach"):
                github.check_token("https://github.com", "tok")
        with mock.patch("urllib.request.urlopen", return_value=_response({})):
            with self.assertRaisesRegex(forge_kinds.ForgeApiError, "whose token"):
                github.check_token("https://github.com", "tok")
        with mock.patch("urllib.request.urlopen",
                        side_effect=[_response({"login": "o"}), _response({"no": "list"})]):
            with self.assertRaisesRegex(forge_kinds.ForgeApiError, "didn't list"):
                github.check_token("https://github.com", "tok")
        with mock.patch("urllib.request.urlopen", return_value=_response({"x": 1})):
            self.assertFalse(github.is_github("https://other.example.com"))
            self.assertTrue(github.is_github("https://github.com"))


class TestGitHubDeployKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch(
            "bot_bottle.contrib.github.deploy_key_provisioner.generate_keypair",
            return_value=(b"PRIVATE", "ssh-ed25519 AAAA pub")))
        self.provisioner = get_provisioner("github", "tok", "https://github.com")

    def test_factory(self) -> None:
        self.assertIsInstance(self.provisioner, GitHubDeployKeyProvisioner)

    def test_create(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({"id": 7})) as urlopen:
            key_id, private = self.provisioner.create("acme/app", "bot-bottle:x:app",
                                                      read_only=True)
        self.assertEqual(("7", b"PRIVATE"), (key_id, private))
        req = urlopen.call_args.args[0]
        self.assertEqual(("POST", "https://api.github.com/repos/acme/app/keys"),
                         (req.get_method(), req.full_url))
        self.assertEqual({"title": "bot-bottle:x:app", "key": "ssh-ed25519 AAAA pub",
                          "read_only": True}, json.loads(req.data))
        self.assertEqual("Bearer tok", req.get_header("Authorization"))

    def test_create_errors(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(422, "in use")):
            with self.assertRaises(DeployKeyCollisionError):
                self.provisioner.create("acme/app", "t")
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404)):
            with self.assertRaisesRegex(RuntimeError, "Administration: read & write"):
                self.provisioner.create("acme/app", "t")
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500 —"):
                self.provisioner.create("acme/app", "t")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaisesRegex(RuntimeError, "down"):
                self.provisioner.create("acme/app", "t")

    def test_delete(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_response({})) as urlopen:
            self.provisioner.delete("acme/app", "7")
        req = urlopen.call_args.args[0]
        self.assertEqual(("DELETE", "https://api.github.com/repos/acme/app/keys/7"),
                         (req.get_method(), req.full_url))
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404)):
            self.provisioner.delete("acme/app", "7")  # already gone
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(403)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                self.provisioner.delete("acme/app", "7")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaisesRegex(RuntimeError, "down"):
                self.provisioner.delete("acme/app", "7")


class TestGitHubForge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with

    @connected(GITHUB)
    def test_bottle_grants_github_repos(self) -> None:
        (self.tmp / "dev.md").write_text(
            "---\nextends: [basic]\n"
            "git_gate:\n  repos: [github.com/diderisoft/bot-bottle-design:write]\n---\n")
        bottle, _ = load_bottle_chain("dev", [(self.tmp, False), (BUILTIN_BOTTLES_DIR, False)])
        entry = bottle.git[0]
        self.assertEqual("ssh://git@github.com/diderisoft/bot-bottle-design.git", entry.Upstream)
        self.assertEqual(("github", "GITHUB_COM_TOKEN", "https://github.com", False),
                         (entry.Key.provider, entry.Key.forge_token_env, entry.Key.api_url,
                          entry.Key.read_only))
        # HTTPS with the user's token by default (ADR 0014): no deploy key.
        self.assertEqual((), deploy_key_refs(bottle))
        (upstream,) = git_gate_upstreams_for_bottle(bottle)
        self.assertEqual(("https", "https://github.com/diderisoft/bot-bottle-design.git",
                          "GITHUB_COM_TOKEN"),
                         (upstream.transport, upstream.upstream_url, upstream.token_secret))

    @connected(replace(GITHUB, users=(replace(GITHUB.users[0], transport="deploy-key"),)))
    def test_a_user_can_opt_into_deploy_keys(self) -> None:
        (self.tmp / "dev.md").write_text(
            "---\nextends: [basic]\n"
            "git_gate:\n  repos: [github.com/diderisoft/bot-bottle-design:write]\n---\n")
        bottle, _ = load_bottle_chain("dev", [(self.tmp, False), (BUILTIN_BOTTLES_DIR, False)])
        self.assertEqual("deploy-key", bottle.git[0].Key.transport)
        (ref,) = deploy_key_refs(bottle)
        self.assertEqual("github", ref.provider)
        (upstream,) = git_gate_upstreams_for_bottle(bottle)
        self.assertEqual(("ssh", "ssh://git@github.com/diderisoft/bot-bottle-design.git"),
                         (upstream.transport, upstream.upstream_url))

    def test_revoke_uses_the_refs_kind(self) -> None:
        ref = DeployKeyRef(name="app", forge_token_env="T", api_url="", upstream_host="github.com",
                           upstream_path="acme/app.git", provider="github")
        secrets = mock.Mock(get=mock.Mock(return_value="tok"))
        with mock.patch("bot_bottle.deploy_key_provisioner.get_provisioner") as factory, \
                contextlib.redirect_stderr(io.StringIO()):
            revoke_key(ref, "7", secrets)
        factory.assert_called_once_with("github", "tok", "https://github.com")
        factory.return_value.delete.assert_called_once_with("acme/app", "7")
        legacy = DeployKeyRef(**{k: v for k, v in ref.__dict__.items() if k != "provider"})
        self.assertEqual("gitea", legacy.provider)

    def test_connect_records_a_github_connection(self) -> None:
        self.addCleanup(use_bottle_root(self.tmp))
        check = forge_kinds.TokenCheck("octo", "ssh://git@github.com", 3, 5)
        with mock.patch.object(github, "check_token", return_value=check) as checked, \
                mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                           return_value="ssh-ed25519 AAAA") as keyscan, \
                mock.patch("bot_bottle.paths.host_secrets_path",
                           return_value=self.tmp / "secrets"), \
                mock.patch("bot_bottle.manifest.versions.record_or_warn"):
            done = connect("https://github.com", "tok", transport="deploy-key")
        checked.assert_called_once_with("https://github.com", "tok")
        keyscan.assert_called_once_with("github.com", "22")
        self.assertEqual(("github.com", "octo", True), (done.key, done.account, done.default))
        conn = connections()["github.com"]
        self.assertEqual(("github", "deploy-key"), (conn.kind, conn.user().transport))

    def test_connect_refuses_another_kind_and_says_what_a_token_needs(self) -> None:
        with self.assertRaisesRegex(ConnectError, "gitlab.com isn't a forge"):
            connect("https://gitlab.com", "tok")
        with mock.patch.object(github, "check_token",
                               side_effect=forge_kinds.ForgeApiError("HTTP 401")), \
                self.assertRaisesRegex(ConnectError, "Administration: read & write"):
            connect("https://github.com", "tok")


class TestCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.tmp / "root"))
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)

    def test_github_says_what_its_token_needs(self) -> None:
        err = io.StringIO()
        check = forge_kinds.TokenCheck("octo", "ssh://git@github.com", 1, 1)
        with mock.patch("getpass.getpass", return_value="tok"), \
                mock.patch.object(github, "check_token", return_value=check), \
                mock.patch("bot_bottle.git_gate.host_key.fetch_host_key",
                           return_value="ssh-ed25519 AAAA"), \
                mock.patch("bot_bottle.manifest.versions.record_or_warn"), \
                contextlib.redirect_stderr(err):
            self.assertEqual(0, cmd_forge(["connect", "github.com", "--token", "--no-browser"]))
        self.assertIn("settings/personal-access-tokens/new", err.getvalue())
        self.assertIn("Administration: read & write", err.getvalue())
        self.assertIn("github.com", connections())

    def test_refuses_another_kind_before_asking_for_a_token(self) -> None:
        err = io.StringIO()
        with mock.patch("getpass.getpass") as ask, contextlib.redirect_stderr(err), \
                self.assertRaises(Die):
            cmd_forge(["connect", "https://bitbucket.org"])
        ask.assert_not_called()
        self.assertIn("bitbucket.org isn't a forge bot-bottle can connect", err.getvalue())


ARRIVED = "2026-10-26T10:00:00+00:00"


def _ask(tool: str, proposed: str) -> SimpleNamespace:
    return SimpleNamespace(id="p1", bottle="b1", session="s", tool=tool, proposed=proposed,
                           justification="need it", arrived=ARRIVED, orchestrator="http://orch")


class TestVet(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(remote_actions, "_connected_forges",
                                            return_value={"git.example.com": "gitea"}))
        self.detect = self.enterContext(mock.patch("bot_bottle.forge_kinds.detect",
                                                   return_value=""))

    def test_forge_connect(self) -> None:
        self.assertEqual(("", "gitea"),
                         remote_actions.vet(_ask("forge-connect", "https://git.example.com")))
        self.detect.assert_not_called()  # a connected forge's kind is known
        refusal, kind = remote_actions.vet(_ask("forge-connect", "https://other.example.com"))
        self.assertEqual(("other.example.com isn't a forge bot-bottle can connect; supported: "
                          "Gitea, GitHub", ""), (refusal, kind))
        self.detect.return_value = "github"
        self.assertEqual(("", "github"),
                         remote_actions.vet(_ask("forge-connect", "https://ghe.example.com")))

    def test_add_repo(self) -> None:
        with mock.patch.object(repo_grants, "find_forge", return_value=object()):
            self.assertEqual(("", ""), remote_actions.vet(_ask("add-repo", "git.example.com/a/b")))
        with mock.patch.object(repo_grants, "find_forge", return_value=None):
            refusal, _ = remote_actions.vet(_ask("add-repo", "code.example.com:3000/a/b"))
            self.assertIn("code.example.com isn't a forge bot-bottle can connect", refusal)
            self.detect.assert_called_with("https://code.example.com:3000")
            self.detect.return_value = "github"
            refusal, _ = remote_actions.vet(_ask("add-repo", "github.com/a/b:write"))
        self.assertEqual("github.com isn't connected to this host; ask for it with forge-connect "
                         "(https://github.com) first, then add-repo again", refusal)

    def test_other_tools_pass(self) -> None:
        self.assertEqual(("", ""), remote_actions.vet(_ask("egress-allow", "x")))


class TestSpecialCard(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.orchestrator = mock.Mock(return_value=self.client)
        self.nudge = self.enterContext(
            mock.patch("bot_bottle.control.hostctl.nudge.nudge_if_waited"))
        self.enterContext(mock.patch.object(remote_actions, "_connected_forges", return_value={}))

    def card(self, request: SimpleNamespace) -> object:
        return asyncio.run(remote_actions.special_card(request, self.orchestrator))

    def test_refuses_for_the_operator(self) -> None:
        request = _ask("forge-connect", "https://gitlab.com")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(([], []), self.card(request))
        self.orchestrator.assert_called_once_with("http://orch")
        kwargs = self.client.supervise_respond.call_args.kwargs
        self.assertEqual(("reject", "b1"), (kwargs["decision"], kwargs["bottle_slug"]))
        self.assertIn("gitlab.com isn't a forge", kwargs["notes"])
        self.nudge.assert_called_once()

    def test_answered_elsewhere_meanwhile(self) -> None:
        self.client.supervise_respond.side_effect = AlreadyAnswered("approved")
        self.assertEqual(([], []), self.card(_ask("forge-connect", "https://gitlab.com")))

    def test_a_failed_refusal_shows_the_card(self) -> None:
        self.client.supervise_respond.side_effect = OSError("down")
        with contextlib.redirect_stderr(io.StringIO()):
            lines, options = self.card(_ask("forge-connect", "https://gitlab.com"))  # type: ignore[misc]
        self.assertEqual([], options)  # nothing to connect: only deny
        self.assertIn("gitlab.com isn't a forge bot-bottle can connect", lines[-1])

    def test_a_github_card_connects_through_the_app(self) -> None:
        lines, options = self.card(_ask("forge-connect", "https://github.com"))  # type: ignore[misc]
        self.assertEqual(["github_authorize"], options)
        text = "\n".join(lines)
        for part in ("kind: GitHub", "app: ", "install: https://github.com/apps/",
                     "new to this host"):
            self.assertIn(part, text)
        for part in ("token page:", "token help:", "scopes:"):
            self.assertNotIn(part, text)
        self.client.supervise_respond.assert_not_called()

    def test_a_github_enterprise_card_still_takes_a_token(self) -> None:
        with mock.patch("bot_bottle.forge_kinds.detect", return_value="github"):
            lines, options = self.card(  # type: ignore[misc]
                _ask("forge-connect", "https://ghe.example.com"))
        self.assertEqual(["forge_connect"], options)
        self.assertIn("token help: a fine-grained token", "\n".join(lines))

    def test_other_requests_have_none(self) -> None:
        self.assertIsNone(self.card(_ask("egress-allow", "routes")))


if __name__ == "__main__":
    unittest.main()
