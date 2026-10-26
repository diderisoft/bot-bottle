"""Forges in bottles and one-line repo grants (PRD 0103)."""

from __future__ import annotations

import unittest
from typing import Any

from bot_bottle.control.git_gate_render import (
    git_gate_render_hook, git_gate_render_provision, git_gate_upstreams_for_bottle,
)
from bot_bottle.manifest.extends import merge_bottles_runtime, resolve_bottles
from bot_bottle.manifest.trust import grants
from bot_bottle.manifest.util import ManifestError

FORGE: Any = {
    "credentials": {"gitea-api": {"source": "secret", "secret": "GITEA_X"}},
    "git-gate": {"forges": {"gitea.example.com": {
        "kind": "gitea", "url": "https://gitea.example.com/",
        "ssh": "ssh://git@10.0.0.1:30009", "token": "$gitea-api",
        "host_key": "ssh-ed25519 AAAA",
    }}},
}


def _resolve(raws: Any, name: str = "dev"):
    return resolve_bottles(raws)[name]


class TestGrants(unittest.TestCase):
    def test_list_of_grants_through_extends(self) -> None:
        bottle = _resolve({"gitea": FORGE, "dev": {"extends": "gitea", "git-gate": {"repos": [
            "gitea.example.com/acme/app:write", "gitea.example.com/acme/lib.git"]}}})
        app, lib = bottle.git
        self.assertEqual(("app", "ssh://git@10.0.0.1:30009/acme/app.git"), (app.Name, app.Upstream))
        self.assertEqual("lib", lib.Name)
        self.assertEqual(("gitea", "GITEA_X", "https://gitea.example.com", False),
                         (app.Key.provider, app.Key.forge_token_env, app.Key.api_url,
                          app.Key.read_only))
        self.assertTrue(lib.Key.read_only)
        self.assertEqual("ssh-ed25519 AAAA", app.KnownHostKey)
        self.assertEqual(["gitea.example.com"], [f.name for f in bottle.forges])
        self.assertEqual([True, False], [u.writable for u in git_gate_upstreams_for_bottle(bottle)])

    def test_mapping_names_a_grant_and_mixes_with_the_long_form(self) -> None:
        bottle = _resolve({"dev": {**FORGE, "git-gate": {**FORGE["git-gate"], "repos": {
            "site": "gitea.example.com/acme/app",
            "static": {"url": "ssh://git@h/x.git", "key": {"provider": "static", "path": "/k"}},
        }}}})
        self.assertEqual(["site", "static"], [e.Name for e in bottle.git])
        self.assertTrue(bottle.git[0].Key.read_only)
        self.assertFalse(bottle.git[1].Key.read_only)

    def test_default_ssh_is_the_web_host(self) -> None:
        forge = {**FORGE, "git-gate": {"forges": {"gitea.example.com": {
            "url": "https://gitea.example.com", "token": "$gitea-api"}}}}
        bottle = _resolve({"dev": {**forge, "git-gate": {**forge["git-gate"],
                                                          "repos": ["gitea.example.com/a/b"]}}})
        self.assertEqual("ssh://git@gitea.example.com/a/b.git", bottle.git[0].Upstream)

    def test_another_account_on_the_same_forge(self) -> None:
        forges = {"gitea.example.com": FORGE["git-gate"]["forges"]["gitea.example.com"],
                  "codex@gitea.example.com": {"url": "https://gitea.example.com",
                                              "token": "$codex-api"}}
        creds = {**FORGE["credentials"], "codex-api": {"source": "secret", "secret": "CODEX_T"}}
        bottle = _resolve({"dev": {"credentials": creds, "git-gate": {
            "forges": forges, "repos": ["codex@gitea.example.com/a/b:write"]}}})
        self.assertEqual("CODEX_T", bottle.git[0].Key.forge_token_env)

    def test_runtime_merge_keeps_forges(self) -> None:
        raws: Any = {"gitea": FORGE, "basic": {}}
        bottles = resolve_bottles(raws)
        merged = merge_bottles_runtime([bottles["basic"], bottles["gitea"]])
        self.assertEqual(["gitea.example.com"], [f.name for f in merged.forges])

    def test_trust_prompt_shows_where_the_token_goes(self) -> None:
        bottle = _resolve({"dev": {**FORGE, "git-gate": {**FORGE["git-gate"], "repos": [
            "gitea.example.com/acme/app"]}}})
        self.assertEqual(["fetch   ssh://git@10.0.0.1:30009/acme/app.git (key minted at "
                          "https://gitea.example.com with $GITEA_X)"], grants(bottle))


class TestErrors(unittest.TestCase):
    def assert_fails(self, raws: Any, fragment: str) -> None:
        with self.assertRaises(ManifestError) as e:
            resolve_bottles(raws)
        self.assertIn(fragment, str(e.exception))

    def test_undeclared_forge(self) -> None:
        self.assert_fails({"dev": {"git-gate": {"repos": ["gitea.example.com/a/b"]}}},
                          "no bottle in this chain declares the forge 'gitea.example.com'")

    def test_bad_grant(self) -> None:
        self.assert_fails({"dev": {**FORGE, "git-gate": {**FORGE["git-gate"],
                                                        "repos": ["gitea.example.com/a"]}}},
                          "<forge>/<owner>/<repo>[:write]")
        self.assert_fails({"dev": {"git-gate": {"repos": [1]}}}, "holds grants")
        self.assert_fails({"dev": {**FORGE, "git-gate": {**FORGE["git-gate"], "repos": [
            "gitea.example.com/a/b", "gitea.example.com/c/b"]}}}, "two grants")

    def test_unknown_credential(self) -> None:
        forge = {"git-gate": FORGE["git-gate"]}
        self.assert_fails({"dev": {**forge, "git-gate": {**forge["git-gate"],
                                                        "repos": ["gitea.example.com/a/b"]}}},
                          "no credential 'gitea-api'")

    def test_shapes(self) -> None:
        def forge(**over: object) -> Any:
            entry = {**FORGE["git-gate"]["forges"]["gitea.example.com"], **over}
            return {"dev": {**FORGE, "git-gate": {"forges": {"gitea.example.com": entry}}}}
        self.assert_fails(forge(kind="gitlab"), "isn't supported yet")
        self.assert_fails(forge(token="GITEA_X"), "as $label")
        self.assert_fails(forge(url="gitea.example.com"), "http(s) URL")
        self.assert_fails(forge(extra=1), "unknown key 'extra'")
        self.assert_fails({"dev": {"git-gate": {"forges": {"a b": {}}}}}, "named by its host")
        self.assert_fails({"dev": {"credentials": {"x": {"source": "prompt"}}}},
                          "isn't supported yet")
        self.assert_fails({"dev": {"credentials": {"Bad": {}}}}, "a label is")
        self.assert_fails({"dev": {"credentials": {"x": {"source": "secret"}}}},
                          "missing 'secret'")
        self.assert_fails({"dev": {"credentials": {"x": {"source": "secret", "secret": "S",
                                                         "kind": "aws"}}}}, "kind 'aws'")
        self.assert_fails({"dev": {"credentials": {"x": {"source": "secret", "y": 1}}}},
                          "unknown key 'y'")

    def test_parents_that_disagree(self) -> None:
        other = {"credentials": {"gitea-api": {"source": "secret", "secret": "OTHER"}}}
        self.assert_fails({"a": FORGE, "b": other, "dev": {"extends": ["a", "b"]}},
                          "credential 'gitea-api' is declared differently")

    def test_a_child_can_override_a_parent(self) -> None:
        other = {"credentials": {"gitea-api": {"source": "secret", "secret": "OTHER"}},
                 "git-gate": {"repos": ["gitea.example.com/a/b"]}}
        bottle = _resolve({"gitea": FORGE, "dev": {"extends": "gitea", **other}})
        self.assertEqual("OTHER", bottle.git[0].Key.forge_token_env)


class TestReadOnlyAtTheGate(unittest.TestCase):
    def test_provision_marks_and_hook_refuses(self) -> None:
        bottle = _resolve({"dev": {**FORGE, "git-gate": {**FORGE["git-gate"], "repos": [
            "gitea.example.com/acme/app"]}}})
        script = git_gate_render_provision("b1", git_gate_upstreams_for_bottle(bottle))
        self.assertIn("ssh://git@10.0.0.1:30009/acme/app.git true", script)
        self.assertIn('config git-gate.readOnly "$read_only"', script)
        self.assertIn("git config --bool git-gate.readOnly", git_gate_render_hook())


if __name__ == "__main__":
    unittest.main()
