"""GitHub repos reach git-gate over HTTPS with the user's token by default
(ADR 0014); a deploy key per launch is opt-in. The token reaches the gateway
only through a 0600 temp file into tmpfs, never a command line or disk, and
the hooks hand it to git only in the environment."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.backend import provision_gateway
from bot_bottle.control.gateway import GatewayProvisionError
from bot_bottle.control.git_gate_render import (
    GitGateUpstream, git_gate_render_access_hook, git_gate_render_hook,
    git_gate_render_provision, git_gate_upstreams_for_bottle,
)
from bot_bottle.control.hostctl.github_git_tokens import GitTokenRenewer
from bot_bottle.git_gate import live_grant
from bot_bottle.git_gate.provision import (
    save_upstreams, saved_upstreams, mints_key,
)
from bot_bottle.manifest import ManifestBottle
from bot_bottle.manifest.document import repo_document
from bot_bottle.manifest.forges import ManifestForge, forges_raw, resolve_forge
from bot_bottle.manifest.git import ManifestGitEntry, ManifestKeyConfig, git_transport
from bot_bottle.manifest.util import ManifestError
from tests.unit._forges import GITHUB, connected
from tests.unit import use_bottle_root

TOKEN = "ghu_" + "x" * 36
HTTPS = GitGateUpstream(name="design", upstream_url="https://github.com/o/design.git",
                        upstream_host="github.com", upstream_port="443", identity_file="",
                        known_host_key="", transport="https", token_secret="GITHUB_COM_TOKEN")


def _entry(transport: str = "", provider: str = "github", read_only: bool = False
           ) -> ManifestGitEntry:
    key: dict[str, object] = {"provider": provider, "forge_token_env": "GITHUB_COM_TOKEN",
                              "api_url": "https://github.com", "read_only": read_only}
    if transport:
        key["transport"] = transport
    return ManifestGitEntry.from_repos_entry(
        "b", "design", {"url": "ssh://git@github.com/o/design.git", "key": key,
                        "host_key": "ssh-ed25519 AAAA"})


class _Transport:
    def __init__(self, fail_tmpfs: bool = False) -> None:
        self.execs: list[list[str]] = []
        self.copied: dict[str, str] = {}
        self._fail_tmpfs = fail_tmpfs

    def exec(self, argv: list[str]) -> None:
        if self._fail_tmpfs and argv[:2] == ["sh", "-c"] and "/proc/mounts" in argv[2]:
            raise GatewayProvisionError("no tmpfs")
        self.execs.append(argv)

    def cp_into(self, src: str, dest: str) -> None:
        self.copied[dest] = Path(src).read_text(encoding="utf-8")
        self.copied[dest + ":mode"] = oct(os.stat(src).st_mode & 0o777)


class TestTransport(unittest.TestCase):
    def test_github_defaults_to_https(self) -> None:
        self.assertEqual("https", git_transport(_entry().Key))
        self.assertFalse(mints_key(_entry().Key))

    def test_deploy_key_is_opt_in(self) -> None:
        key = _entry("deploy-key").Key
        self.assertEqual(("ssh", True), (git_transport(key), mints_key(key)))

    def test_gitea_keeps_deploy_keys(self) -> None:
        key = ManifestKeyConfig(provider="gitea", forge_token_env="T")
        self.assertEqual(("ssh", True), (git_transport(key), mints_key(key)))

    def test_bad_transports_are_refused(self) -> None:
        for transport, provider in (("ftp", "github"), ("https", "gitea")):
            with self.subTest(transport=transport, provider=provider), \
                    self.assertRaises(ManifestError):
                _entry(transport, provider)

    def test_the_users_transport_is_carried_and_a_bottle_cant_set_it(self) -> None:
        user = replace(GITHUB.users[0], transport="deploy-key")
        with connected(replace(GITHUB, users=(user,))):
            forge = resolve_forge("github.com", {"kind": "github"}, "b")
        self.assertEqual("deploy-key", forge.key(True)["transport"])
        self.assertNotIn("transport", ManifestForge(
            name="g", kind="github", url="u", ssh="s", token_secret="T").key(True))
        with self.assertRaisesRegex(ManifestError, "the host's connection now"):
            forges_raw("b", {"git_gate": {"forges": {"github.com": {
                "kind": "github", "transport": "deploy-key"}}}})

    def test_document_round_trips_the_transport(self) -> None:
        self.assertEqual("deploy-key", repo_document(_entry("deploy-key"))["key"]["transport"])  # type: ignore[index]
        self.assertNotIn("transport", repo_document(_entry())["key"])  # type: ignore[operator]

    def test_the_upstream_is_https_with_no_key(self) -> None:
        (u,) = git_gate_upstreams_for_bottle(ManifestBottle(git=(_entry(read_only=True),)))
        self.assertEqual(("https", "https://github.com/o/design.git", "", "", False,
                          "GITHUB_COM_TOKEN"),
                         (u.transport, u.upstream_url, u.identity_file, u.known_host_key,
                          u.writable, u.token_secret))

    def test_the_host_db_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.addCleanup(use_bottle_root(Path(d)))
            save_upstreams("s", (HTTPS,))
            (u,) = saved_upstreams("s")
        self.assertEqual(("https", "GITHUB_COM_TOKEN"), (u.transport, u.token_secret))


class TestProvisionRender(unittest.TestCase):
    def _render(self, *ups: GitGateUpstream) -> str:
        return git_gate_render_provision("b1", ups, trailer_mode="", trailer="",
                                         baseline="/git-gate/creds/b1/baseline-commits")

    def test_https_repo_names_its_tmpfs_token(self) -> None:
        line = next(l for l in self._render(HTTPS).splitlines() if l.startswith("init_repo "))
        self.assertEqual("init_repo design https://github.com/o/design.git false https "
                         "/dev/shm/git-gate/b1/GITHUB_COM_TOKEN-token false", line)
        self.assertNotIn(TOKEN, self._render(HTTPS))

    def test_unsafe_secret_names_are_refused(self) -> None:
        import dataclasses  # pylint: disable=import-outside-toplevel
        with self.assertRaises(ValueError):
            self._render(dataclasses.replace(HTTPS, token_secret="a;rm -rf /"))

    def test_the_remote_follows_the_transport(self) -> None:
        self.assertIn('remote set-url origin "$upstream_url"', self._render(HTTPS))


class TestPushTokens(unittest.TestCase):
    def test_into_tmpfs_by_file_never_argv(self) -> None:
        t = _Transport()
        provision_gateway.push_tokens(t, "b1", (HTTPS,), token_for=lambda s: TOKEN,
                                      expires_for=lambda s: None)
        dest = "/dev/shm/git-gate/b1/GITHUB_COM_TOKEN-token"
        self.assertEqual(TOKEN, t.copied[dest])
        self.assertEqual("0o600", t.copied[dest + ":mode"])
        self.assertIn(["chmod", "600", dest], t.execs)
        self.assertTrue(any("/proc/mounts" in " ".join(a) for a in t.execs))
        self.assertFalse(any(TOKEN in " ".join(a) for a in t.execs))
        self.assertFalse(list(Path(tempfile.gettempdir()).glob("bb-gh-token-*")))

    def test_refuses_without_tmpfs(self) -> None:
        with self.assertRaises(GatewayProvisionError) as ctx:
            provision_gateway.push_tokens(_Transport(fail_tmpfs=True), "b1", (HTTPS,),
                                          token_for=lambda s: TOKEN,
                                      expires_for=lambda s: None)
        self.assertIn("tmpfs", str(ctx.exception))

    def test_no_token_says_how_to_connect(self) -> None:
        with self.assertRaises(GatewayProvisionError) as ctx:
            provision_gateway.push_tokens(_Transport(), "b1", (HTTPS,), token_for=lambda s: None,
                                          expires_for=lambda s: None)
        self.assertIn("forge connect github", str(ctx.exception))

    def test_ssh_only_bottles_push_nothing(self) -> None:
        t = _Transport()
        ssh = GitGateUpstream(name="r", upstream_url="ssh://git@h/o/r.git", upstream_host="h",
                              upstream_port="22", identity_file="/k", known_host_key="k")
        provision_gateway.push_tokens(t, "b1", (ssh,), token_for=lambda s: TOKEN,
                                      expires_for=lambda s: None)
        self.assertEqual(([], {}), (t.execs, t.copied))

    def test_teardown_removes_the_token(self) -> None:
        t = _Transport()
        provision_gateway.deprovision_git_gate(t, "b1")
        self.assertIn("/dev/shm/git-gate/b1", t.execs[0])


@unittest.skipUnless(shutil.which("git") and shutil.which("base64"), "needs git and base64")
class TestHooksUseTheToken(unittest.TestCase):
    """The access hook against a repo set up for HTTPS: a stand-in `git` on
    PATH records how the network calls were made, and passes the rest on."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.repo = self.tmp / "design.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.repo)], check=True)
        self.token_file = self.tmp / "tok"
        for k, v in (("git-gate.transport", "https"), ("git-gate.tokenFile", str(self.token_file))):
            subprocess.run(["git", "-C", str(self.repo), "config", k, v], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/o/design.git"], check=True)
        real = shutil.which("git")
        self.log = self.tmp / "net.log"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(
            "#!/bin/sh\ncase \" $* \" in\n  *' fetch '*|*' ls-remote '*)\n"
            f"    printf 'argv:%s\\nheader:%s\\n' \"$*\" \"$GIT_CONFIG_VALUE_0\" >> {self.log}\n"
            "    exit 0;;\nesac\n"
            f"exec {real} \"$@\"\n")
        (bin_dir / "git").chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        self.hook = self.tmp / "access-hook"
        self.hook.write_text(git_gate_render_access_hook())
        self.hook.chmod(0o755)

    def _run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.hook), "upload-pack", str(self.repo), "h", "a"],
                              env=self.env, capture_output=True, text=True, check=False)

    def test_the_token_goes_in_the_env_not_argv(self) -> None:
        self.token_file.write_text(TOKEN)
        r = self._run()
        self.assertEqual(0, r.returncode, r.stderr)
        log = self.log.read_text()
        self.assertIn("header:Authorization: Basic ", log)
        import base64  # pylint: disable=import-outside-toplevel
        self.assertIn(base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(), log)
        argv_lines = [line for line in log.splitlines() if line.startswith("argv:")]
        self.assertFalse(any(TOKEN in line for line in argv_lines))
        self.assertNotIn(TOKEN, r.stderr)

    def test_no_token_refuses_the_fetch(self) -> None:
        r = self._run()
        self.assertEqual(1, r.returncode)
        self.assertIn("no GitHub token", r.stderr)


class TestPreReceive(unittest.TestCase):
    def test_read_only_is_refused_before_any_upstream_push(self) -> None:
        hook = git_gate_render_hook()
        self.assertLess(hook.index("git-gate.readOnly"), hook.index("upstream_git push"))

    def test_https_failures_say_why(self) -> None:
        hook = git_gate_render_hook()
        for hint in ("--reauthorize", "Contents: read & write", "install the bot-bottle app"):
            self.assertIn(hint, hook)
        self.assertNotIn("GIT_SSH_COMMAND=\"$ssh_cmd\" git push", hook)


class TestLiveGrant(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(root))

    def test_an_https_grant_mints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(live_grant, "_provision_dynamic_key") as mint:
            up = live_grant.provision_live(_entry(), slug="s", stage_dir=Path(d),
                                           secrets=mock.Mock(), grant="github.com/o/design")
            (saved,) = saved_upstreams("s")
        mint.assert_not_called()
        self.assertEqual(("https", "https"), (up.transport, saved.transport))

    def test_regranting_with_other_access_replaces_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            live_grant.provision_live(_entry(read_only=True), slug="s", stage_dir=Path(d),
                                      secrets=mock.Mock(), grant="github.com/o/design")
            live_grant.provision_live(_entry(), slug="s", stage_dir=Path(d),
                                      secrets=mock.Mock(), grant="github.com/o/design:write")
            (saved,) = saved_upstreams("s")
        self.assertTrue(saved.writable)


class TestRenewer(unittest.TestCase):
    def _renewer(self, tokens: list[str], transport: Any) -> tuple[GitTokenRenewer, Path]:
        stage = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(stage))
        save_upstreams("s1", (HTTPS,))
        bottles = SimpleNamespace(sessions=SimpleNamespace(
            records=lambda: [SimpleNamespace(id="s1")]))
        self.enterContext(mock.patch(
            "bot_bottle.control.hostctl.github_git_tokens.adoption.read",
            return_value=SimpleNamespace(registry_id="r1")))
        it = iter(tokens)
        return GitTokenRenewer(bottles, token_for=lambda s: next(it),
                               transport_for=lambda i: transport,
                               expires_for=lambda s: None), stage

    def test_pushes_only_a_changed_token(self) -> None:
        t = _Transport()
        renewer, _ = self._renewer([TOKEN, TOKEN, TOKEN + "2"], t)
        self.assertEqual(["s1"], renewer.renew_all())
        self.assertEqual([], renewer.renew_all())      # unchanged: not copied again
        self.assertEqual(["s1"], renewer.renew_all())  # renewed: pushed
        self.assertEqual(TOKEN + "2", t.copied["/dev/shm/git-gate/r1/GITHUB_COM_TOKEN-token"])

    def test_a_failure_is_logged_and_the_pass_goes_on(self) -> None:
        class _Broken(_Transport):
            def cp_into(self, src: str, dest: str) -> None:
                raise GatewayProvisionError("down")
        renewer, _ = self._renewer([TOKEN], _Broken())
        with mock.patch("bot_bottle.control.hostctl.github_git_tokens.log.warn") as warned:
            self.assertEqual([], renewer.renew_all())
        warned.assert_called_once()


if __name__ == "__main__":
    unittest.main()
