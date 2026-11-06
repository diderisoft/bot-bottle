"""Forges described in bottles, connected on hosts, and one-line repo grants
(PRD 0103, ADR 0019)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from bot_bottle.control.git_gate_render import (
    git_gate_render_hook, git_gate_render_provision, git_gate_upstreams_for_bottle,
)
from bot_bottle.forge_connections import ForgeUser
from bot_bottle.manifest.extends import merge_bottles_runtime, resolve_bottles
from bot_bottle.manifest.forges import with_forge_users
from bot_bottle.manifest.index import ManifestIndex
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from bot_bottle.manifest.trust import (
    UntrustedBottleError, check as trust_check, forge_lines, grants, prompt_text, trust,
)
from bot_bottle.manifest.util import ManifestError
from tests.unit import use_bottle_root
from tests.unit._forges import GITEA, connected


def _trailer_kwargs(bottle_id: str) -> dict[str, str]:
    """The trailer settings every provisioned repo gets."""
    return {"trailer_mode": "require", "trailer": "dev/demo-abc12",
            "baseline": f"/git-gate/creds/{bottle_id}/baseline-commits"}


# A bottle's description of the forge (ADR 0019): never a token.
FORGE: Any = {"git-gate": {"forges": {"gitea.example.com": {
    "kind": "gitea", "url": "https://gitea.example.com/", "ssh": "ssh://git@gitea.example.com:2222",
}}}}
TWO_USERS = replace(GITEA, users=(*GITEA.users, ForgeUser(name="codex", token_secret="CODEX_T")))


def _resolve(raws: Any, name: str = "dev"):
    return resolve_bottles(raws)[name]


@connected(GITEA)
class TestGrants(unittest.TestCase):
    def test_a_grant_needs_no_forge_in_its_chain(self) -> None:
        bottle = _resolve({"dev": {"git-gate": {"repos": [
            "gitea.example.com/acme/app:write", "gitea.example.com/acme/lib.git"]}}})
        app, lib = bottle.git
        # The host's connection, not a description, decides where it goes.
        self.assertEqual(("app", "ssh://git@10.0.0.1:30009/acme/app.git"), (app.Name, app.Upstream))
        self.assertEqual("lib", lib.Name)
        self.assertEqual(("gitea", "GITEA_X", "https://gitea.example.com", False),
                         (app.Key.provider, app.Key.forge_token_env, app.Key.api_url,
                          app.Key.read_only))
        self.assertTrue(lib.Key.read_only)
        self.assertEqual("ssh-ed25519 AAAA", app.KnownHostKey)
        self.assertEqual([("gitea.example.com", "bot")],
                         [(f.name, f.account) for f in bottle.forges])
        self.assertEqual([True, False], [u.writable for u in git_gate_upstreams_for_bottle(bottle)])

    def test_a_description_doesnt_redirect_the_connection(self) -> None:
        bottle = _resolve({"dev": {**FORGE, "git-gate": {**FORGE["git-gate"], "repos": [
            "gitea.example.com/acme/app"]}}})
        self.assertEqual("ssh://git@10.0.0.1:30009/acme/app.git", bottle.git[0].Upstream)

    def test_mapping_names_a_grant_and_mixes_with_the_long_form(self) -> None:
        bottle = _resolve({"dev": {"git-gate": {"repos": {
            "site": "gitea.example.com/acme/app",
            "static": {"url": "ssh://git@h/x.git", "key": {"provider": "static", "path": "/k"}},
        }}}})
        self.assertEqual(["site", "static"], [e.Name for e in bottle.git])
        self.assertTrue(bottle.git[0].Key.read_only)
        self.assertFalse(bottle.git[1].Key.read_only)

    def test_runtime_merge_keeps_forges(self) -> None:
        raws: Any = {"dev": {"git-gate": {"repos": ["gitea.example.com/a/b"]}}, "basic": {}}
        bottles = resolve_bottles(raws)
        merged = merge_bottles_runtime([bottles["basic"], bottles["dev"]])
        self.assertEqual(["gitea.example.com"], [f.name for f in merged.forges])

    def test_described_and_connected_forges_reach_the_bottle(self) -> None:
        bottle = _resolve({"dev": FORGE})
        self.assertEqual(["gitea.example.com"], [f.name for f in bottle.forges])

    def test_trust_prompt_shows_where_the_token_goes(self) -> None:
        bottle = _resolve({"dev": {"git-gate": {"repos": ["gitea.example.com/acme/app"]}}})
        self.assertEqual(["fetch   ssh://git@10.0.0.1:30009/acme/app.git (key minted at "
                          "https://gitea.example.com with $GITEA_X)"], grants(bottle))
        self.assertEqual(["forge   gitea.example.com: gitea at https://gitea.example.com, ssh "
                          "ssh://git@10.0.0.1:30009, host key "
                          "SHA256:cJ6AyISHokEeHuTfufIqhhSS0gxHZRUMDHlKvXD4FHw, as bot"],
                         forge_lines(bottle))

    def test_an_agent_picks_another_user(self) -> None:
        with connected(TWO_USERS):
            bottle = _resolve({"dev": {"git-gate": {"repos": ["gitea.example.com/a/b:write"]}}})
            self.assertEqual("GITEA_X", bottle.git[0].Key.forge_token_env)
            chosen = with_forge_users(bottle, {"gitea.example.com": "codex"})
        self.assertEqual("CODEX_T", chosen.git[0].Key.forge_token_env)
        self.assertEqual("codex", chosen.forges[0].account)

    def test_an_agent_naming_a_user_not_connected(self) -> None:
        bottle = _resolve({"dev": {"git-gate": {"repos": ["gitea.example.com/a/b"]}}})
        with self.assertRaises(ManifestError) as e:
            with_forge_users(bottle, {"gitea.example.com": "nobody"})
        self.assertIn("nobody isn't connected on gitea.example.com", str(e.exception))


@connected(GITEA)
class TestErrors(unittest.TestCase):
    def assert_fails(self, raws: Any, fragment: str) -> None:
        with self.assertRaises(ManifestError) as e:
            resolve_bottles(raws)
        self.assertIn(fragment, str(e.exception))

    def test_a_forge_not_connected(self) -> None:
        self.assert_fails({"dev": {"git-gate": {"repos": ["other.example.com/a/b"]}}},
                          "the forge other.example.com isn't connected on this host; connect "
                          "it with `bot-bottle forge connect https://other.example.com`")
        described = {"git-gate": {"forges": {"other.example.com": {
            "kind": "gitea", "url": "https://other.example.com:3000"}},
            "repos": ["other.example.com/a/b"]}}
        self.assert_fails({"dev": described}, "url must be on other.example.com")

    def test_the_description_must_agree_with_the_connection(self) -> None:
        def grant(**over: object) -> Any:
            entry = {"kind": "gitea", **over}
            return {"dev": {"git-gate": {"forges": {"gitea.example.com": entry},
                                         "repos": ["gitea.example.com/a/b"]}}}
        self.assert_fails(grant(kind="github"), "this host connected it as gitea")
        self.assert_fails(grant(host_key="ssh-ed25519 OTHER"), "isn't the one this host pinned")
        _resolve(grant(host_key="ssh-ed25519 AAAA"))

    def test_a_token_is_the_hosts(self) -> None:
        self.assert_fails({"dev": {"credentials": {"x": {"source": "secret", "secret": "S"}}}},
                          "a forge's token is the host's connection now")
        entry = {**FORGE["git-gate"]["forges"]["gitea.example.com"], "token": "$x"}
        self.assert_fails({"dev": {"git-gate": {"forges": {"gitea.example.com": entry}}}},
                          "bot-bottle forge connect")

    def test_bad_grant(self) -> None:
        self.assert_fails({"dev": {"git-gate": {"repos": ["gitea.example.com/a"]}}},
                          "<forge>/<owner>/<repo>[:write]")
        self.assert_fails({"dev": {"git-gate": {"repos": [1]}}}, "holds grants")
        self.assert_fails({"dev": {"git-gate": {"repos": [
            "gitea.example.com/a/b", "gitea.example.com/c/b"]}}}, "two grants")
        self.assert_fails({"dev": {"git-gate": {"repos": ["codex@gitea.example.com/a/b"]}}},
                          "forge_users")

    def test_shapes(self) -> None:
        def forge(**over: object) -> Any:
            entry = {**FORGE["git-gate"]["forges"]["gitea.example.com"], **over}
            return {"dev": {"git-gate": {"forges": {"gitea.example.com": entry}}}}
        self.assert_fails(forge(kind="gitlab"), "isn't supported yet")
        self.assert_fails(forge(url="gitea.example.com"), "http(s) URL")
        self.assert_fails(forge(extra=1), "unknown key 'extra'")
        self.assert_fails({"dev": {"git-gate": {"forges": {"a b": {}}}}}, "named by its host")

    def test_parents_that_disagree(self) -> None:
        other = {"git-gate": {"forges": {"gitea.example.com": {"kind": "gitea"}}}}
        self.assert_fails({"a": FORGE, "b": other, "dev": {"extends": ["a", "b"]}},
                          "is declared differently")

    def test_two_users_and_no_default(self) -> None:
        nobody = replace(TWO_USERS, users=tuple(replace(u, default=False)
                                                for u in TWO_USERS.users))
        with connected(nobody):
            self.assert_fails({"dev": {"git-gate": {"repos": ["gitea.example.com/a/b"]}}},
                              "none is the default")


class TestReadOnlyAtTheGate(unittest.TestCase):
    @connected(GITEA)
    def test_provision_marks_and_hook_refuses(self) -> None:
        bottle = _resolve({"dev": {"git-gate": {"repos": ["gitea.example.com/acme/app"]}}})
        script = git_gate_render_provision("b1", git_gate_upstreams_for_bottle(bottle),
                                           **_trailer_kwargs("b1"))
        self.assertIn("ssh://git@10.0.0.1:30009/acme/app.git true", script)
        self.assertIn('config git-gate.readOnly "$read_only"', script)
        self.assertIn("git config --bool git-gate.readOnly", git_gate_render_hook())


class TestTrustCoversTheConnection(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(tmp / "root"))
        self.project = tmp / "app" / ".bot-bottle" / "bottles"
        self.project.mkdir(parents=True)
        (self.project / "main.md").write_text(
            "---\ngit-gate:\n  repos: [gitea.example.com/acme/app:write]\n---\n")
        self.dirs = [(self.project, True), (BUILTIN_BOTTLES_DIR, False)]

    def check(self) -> None:
        bottle, sources = load_bottle_chain("./main", self.dirs)
        trust_check("./main", bottle, sources)

    def test_reconnecting_differently_asks_again(self) -> None:
        with connected(GITEA):
            with self.assertRaises(UntrustedBottleError) as first:
                self.check()
            self.assertIn("forge   gitea.example.com: gitea", "\n".join(first.exception.grants))
            trust(first.exception)
            self.check()  # trusted
        with connected(replace(GITEA, ssh="ssh://git@10.9.9.9:22")):
            with self.assertRaises(UntrustedBottleError) as again:
                self.check()
        text = prompt_text(again.exception)
        self.assertIn("+ forge   gitea.example.com: gitea at https://gitea.example.com, ssh "
                      "ssh://git@10.9.9.9:22", text)
        self.assertIn("- forge   gitea.example.com", text)

    def test_trusted_as_the_account_the_agent_picks(self) -> None:
        root = self.project.parent.parent.parent / "root"
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "plain.md").write_text("---\n---\n")
        (self.project.parent / "agents").mkdir()
        (self.project.parent / "agents" / "codex.md").write_text(
            "---\nforge_users:\n  gitea.example.com: codex\n---\n")
        two = replace(GITEA, users=(*GITEA.users, ForgeUser("codex", "CODEX_X")))
        index = ManifestIndex.resolve(str(self.project.parent.parent))
        with connected(two):
            with self.assertRaises(UntrustedBottleError) as first:
                index.load_for_agent("plain", ("./main",))
            trust(first.exception)
            index.load_for_agent("plain", ("./main",))  # trusted as bot, the default
            with self.assertRaises(UntrustedBottleError) as again:
                index.load_for_agent("codex", ("./main",))
            text = prompt_text(again.exception)
            self.assertIn("as bot", text)
            self.assertIn("as codex", text)
            trust(again.exception)
            manifest = index.load_for_agent("codex", ("./main",))
        self.assertEqual("CODEX_X", manifest.bottle.forges[0].token_secret)


if __name__ == "__main__":
    unittest.main()
