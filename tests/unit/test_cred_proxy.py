"""Unit: CredProxy upstream lift + routes.json render + token resolution
(PRD 0010)."""

import json
import unittest

from claude_bottle.cred_proxy import (
    cred_proxy_render_routes,
    cred_proxy_resolve_token_values,
    cred_proxy_token_env_map,
    cred_proxy_upstreams_for_bottle,
)
from claude_bottle.log import Die
from claude_bottle.manifest import Manifest


def _bottle(routes):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"cred_proxy": {"routes": routes}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


class TestUpstreamLift(unittest.TestCase):
    def test_single_route_yields_single_upstream(self):
        b = _bottle([
            {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer", "token_ref": "CLAUDE_BOTTLE_OAUTH_TOKEN",
             "role": "anthropic-base-url"},
        ])
        upstreams = cred_proxy_upstreams_for_bottle(b)
        self.assertEqual(1, len(upstreams))
        u = upstreams[0]
        self.assertEqual("/anthropic/", u.path)
        self.assertEqual("https://api.anthropic.com", u.upstream)
        self.assertEqual("Bearer", u.auth_scheme)
        self.assertEqual("CRED_PROXY_TOKEN_0", u.token_env)
        self.assertEqual("CLAUDE_BOTTLE_OAUTH_TOKEN", u.token_ref)
        self.assertEqual(("anthropic-base-url",), u.roles)

    def test_shared_token_ref_collapses_to_one_slot(self):
        # Two github routes share GH_PAT — they share token_env.
        b = _bottle([
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_ref": "GH_PAT"},
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GH_PAT",
             "role": "git-insteadof"},
        ])
        upstreams = cred_proxy_upstreams_for_bottle(b)
        self.assertEqual(2, len(upstreams))
        self.assertEqual({"CRED_PROXY_TOKEN_0"},
                         {u.token_env for u in upstreams})

    def test_distinct_token_refs_get_distinct_slots(self):
        b = _bottle([
            {"path": "/a/", "upstream": "https://a.example",
             "auth_scheme": "Bearer", "token_ref": "T1"},
            {"path": "/b/", "upstream": "https://b.example",
             "auth_scheme": "Bearer", "token_ref": "T2"},
            {"path": "/c/", "upstream": "https://c.example",
             "auth_scheme": "Bearer", "token_ref": "T1"},
        ])
        upstreams = cred_proxy_upstreams_for_bottle(b)
        # T1 -> slot 0, T2 -> slot 1, T1 reuses slot 0.
        self.assertEqual("CRED_PROXY_TOKEN_0", upstreams[0].token_env)
        self.assertEqual("CRED_PROXY_TOKEN_1", upstreams[1].token_env)
        self.assertEqual("CRED_PROXY_TOKEN_0", upstreams[2].token_env)

    def test_upstream_trailing_slash_stripped(self):
        b = _bottle([
            {"path": "/x/", "upstream": "https://gitea.dideric.is/",
             "auth_scheme": "token", "token_ref": "T"},
        ])
        self.assertEqual("https://gitea.dideric.is",
                         cred_proxy_upstreams_for_bottle(b)[0].upstream)

    def test_roles_list_passes_through(self):
        b = _bottle([
            {"path": "/gitea/x/", "upstream": "https://gitea.example.com",
             "auth_scheme": "token", "token_ref": "T",
             "role": ["git-insteadof", "tea-login"]},
        ])
        self.assertEqual(("git-insteadof", "tea-login"),
                         cred_proxy_upstreams_for_bottle(b)[0].roles)

    def test_empty_routes_yields_empty_upstreams(self):
        b = _bottle([])
        self.assertEqual((), cred_proxy_upstreams_for_bottle(b))


class TestTokenEnvMap(unittest.TestCase):
    def test_distinct_envs_yield_full_map(self):
        b = _bottle([
            {"path": "/a/", "upstream": "https://a.example",
             "auth_scheme": "Bearer", "token_ref": "A"},
            {"path": "/b/", "upstream": "https://b.example",
             "auth_scheme": "Bearer", "token_ref": "B"},
        ])
        m = cred_proxy_token_env_map(cred_proxy_upstreams_for_bottle(b))
        self.assertEqual({"CRED_PROXY_TOKEN_0": "A",
                          "CRED_PROXY_TOKEN_1": "B"}, m)

    def test_shared_token_ref_yields_one_env(self):
        b = _bottle([
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_ref": "GH"},
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GH"},
        ])
        m = cred_proxy_token_env_map(cred_proxy_upstreams_for_bottle(b))
        self.assertEqual({"CRED_PROXY_TOKEN_0": "GH"}, m)


class TestRoutesRender(unittest.TestCase):
    def test_renders_json_with_expected_shape(self):
        b = _bottle([
            {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer", "token_ref": "CLAUDE_BOTTLE_OAUTH_TOKEN"},
            {"path": "/gitea/x/", "upstream": "https://gitea.dideric.is",
             "auth_scheme": "token", "token_ref": "GITEA_TOKEN"},
        ])
        rendered = cred_proxy_render_routes(cred_proxy_upstreams_for_bottle(b))
        payload = json.loads(rendered)
        self.assertEqual(["routes"], list(payload.keys()))
        self.assertEqual(2, len(payload["routes"]))
        first = payload["routes"][0]
        self.assertEqual({"path", "upstream", "auth_scheme", "token_env"},
                         set(first.keys()))

    def test_routes_carry_no_token_values_or_host_env_names(self):
        # routes.json lives mode-600 in the staging dir and gets
        # docker cp'd into the sidecar — it must not leak secret values
        # or the host-side TokenRef name.
        b = _bottle([{"path": "/x/", "upstream": "https://x.example",
                      "auth_scheme": "Bearer", "token_ref": "GITHUB_TOKEN"}])
        rendered = cred_proxy_render_routes(cred_proxy_upstreams_for_bottle(b))
        self.assertNotIn("GITHUB_TOKEN", rendered)

    def test_empty_upstreams_renders_empty_routes_array(self):
        rendered = cred_proxy_render_routes(())
        self.assertEqual({"routes": []}, json.loads(rendered))


class TestResolveTokenValues(unittest.TestCase):
    def test_resolves_present_env(self):
        out = cred_proxy_resolve_token_values(
            {"CRED_PROXY_TOKEN_0": "FOO"},
            {"FOO": "the-value"},
        )
        self.assertEqual({"CRED_PROXY_TOKEN_0": "the-value"}, out)

    def test_unset_host_env_dies(self):
        with self.assertRaises(Die):
            cred_proxy_resolve_token_values(
                {"CRED_PROXY_TOKEN_0": "MISSING"},
                {},
            )

    def test_empty_host_env_dies(self):
        with self.assertRaises(Die):
            cred_proxy_resolve_token_values(
                {"CRED_PROXY_TOKEN_0": "FOO"},
                {"FOO": ""},
            )


class TestCredProxyPrepare(unittest.TestCase):
    def test_prepare_writes_routes_file_and_returns_plan(self):
        import tempfile
        from pathlib import Path

        from claude_bottle.cred_proxy import CredProxy, CredProxyPlan

        class StubCredProxy(CredProxy):
            def start(self, plan): return ""
            def stop(self, target): return None

        b = _bottle([
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_ref": "GITHUB_TOKEN"},
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GITHUB_TOKEN",
             "role": "git-insteadof"},
        ])
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            plan = StubCredProxy().prepare(b, "test-slug", stage)
            self.assertIsInstance(plan, CredProxyPlan)
            self.assertEqual("test-slug", plan.slug)
            self.assertTrue(plan.routes_path.is_file())
            self.assertEqual(0o600, plan.routes_path.stat().st_mode & 0o777)
            payload = json.loads(plan.routes_path.read_text())
            self.assertEqual(2, len(payload["routes"]))
            self.assertEqual({"CRED_PROXY_TOKEN_0": "GITHUB_TOKEN"},
                             plan.token_env_map)


if __name__ == "__main__":
    unittest.main()
