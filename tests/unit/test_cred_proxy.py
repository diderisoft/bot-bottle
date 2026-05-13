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


def _bottle(tokens):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"tokens": tokens}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


class TestUpstreamLift(unittest.TestCase):
    def test_anthropic_yields_one_route(self):
        b = _bottle([{"Kind": "anthropic", "TokenRef": "CLAUDE_BOTTLE_OAUTH_TOKEN"}])
        upstreams = cred_proxy_upstreams_for_bottle(b)
        self.assertEqual(1, len(upstreams))
        u = upstreams[0]
        self.assertEqual("anthropic", u.kind)
        self.assertEqual("/anthropic/", u.path)
        self.assertEqual("https://api.anthropic.com", u.upstream)
        self.assertEqual("Bearer", u.auth_scheme)
        self.assertEqual("CRED_PROXY_TOKEN_0", u.token_env)
        self.assertEqual("CLAUDE_BOTTLE_OAUTH_TOKEN", u.token_ref)

    def test_github_yields_two_routes_sharing_token_env(self):
        b = _bottle([{"Kind": "github", "TokenRef": "GITHUB_TOKEN"}])
        upstreams = cred_proxy_upstreams_for_bottle(b)
        self.assertEqual(2, len(upstreams))
        paths = [u.path for u in upstreams]
        self.assertIn("/gh-api/", paths)
        self.assertIn("/gh-git/", paths)
        self.assertEqual({"CRED_PROXY_TOKEN_0"}, {u.token_env for u in upstreams})
        for u in upstreams:
            self.assertEqual("Bearer", u.auth_scheme)
            self.assertEqual("GITHUB_TOKEN", u.token_ref)

    def test_gitea_uses_token_scheme_and_host_path(self):
        b = _bottle([
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is"},
        ])
        u = cred_proxy_upstreams_for_bottle(b)[0]
        self.assertEqual("/gitea/gitea.dideric.is/", u.path)
        self.assertEqual("https://gitea.dideric.is", u.upstream)
        self.assertEqual("token", u.auth_scheme)

    def test_gitea_url_trailing_slash_stripped(self):
        b = _bottle([
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is/"},
        ])
        u = cred_proxy_upstreams_for_bottle(b)[0]
        self.assertEqual("https://gitea.dideric.is", u.upstream)

    def test_npm_yields_one_route(self):
        b = _bottle([{"Kind": "npm", "TokenRef": "NPM_TOKEN"}])
        u = cred_proxy_upstreams_for_bottle(b)[0]
        self.assertEqual("/npm/", u.path)
        self.assertEqual("https://registry.npmjs.org", u.upstream)

    def test_four_kinds_get_distinct_token_envs(self):
        b = _bottle([
            {"Kind": "anthropic", "TokenRef": "A"},
            {"Kind": "github", "TokenRef": "G"},
            {"Kind": "gitea", "TokenRef": "T",
             "Url": "https://gitea.dideric.is"},
            {"Kind": "npm", "TokenRef": "N"},
        ])
        upstreams = cred_proxy_upstreams_for_bottle(b)
        # 1 anthropic + 2 github + 1 gitea + 1 npm = 5 routes
        self.assertEqual(5, len(upstreams))
        # github shares one token_env across its two routes -> 4 distinct
        envs = {u.token_env for u in upstreams}
        self.assertEqual({"CRED_PROXY_TOKEN_0", "CRED_PROXY_TOKEN_1",
                          "CRED_PROXY_TOKEN_2", "CRED_PROXY_TOKEN_3"}, envs)

    def test_empty_tokens_yields_empty_upstreams(self):
        b = _bottle([])
        self.assertEqual((), cred_proxy_upstreams_for_bottle(b))


class TestTokenEnvMap(unittest.TestCase):
    def test_distinct_envs_yield_full_map(self):
        b = _bottle([
            {"Kind": "anthropic", "TokenRef": "A"},
            {"Kind": "github", "TokenRef": "G"},
        ])
        m = cred_proxy_token_env_map(cred_proxy_upstreams_for_bottle(b))
        self.assertEqual({"CRED_PROXY_TOKEN_0": "A",
                          "CRED_PROXY_TOKEN_1": "G"}, m)

    def test_github_two_routes_coalesce_to_one_env(self):
        b = _bottle([{"Kind": "github", "TokenRef": "G"}])
        m = cred_proxy_token_env_map(cred_proxy_upstreams_for_bottle(b))
        self.assertEqual({"CRED_PROXY_TOKEN_0": "G"}, m)


class TestRoutesRender(unittest.TestCase):
    def test_renders_json_with_expected_shape(self):
        b = _bottle([
            {"Kind": "anthropic", "TokenRef": "CLAUDE_BOTTLE_OAUTH_TOKEN"},
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is"},
        ])
        rendered = cred_proxy_render_routes(cred_proxy_upstreams_for_bottle(b))
        payload = json.loads(rendered)
        self.assertEqual(["routes"], list(payload.keys()))
        self.assertEqual(2, len(payload["routes"]))
        anthropic = payload["routes"][0]
        self.assertEqual({"path", "upstream", "auth_scheme", "token_env"},
                         set(anthropic.keys()))
        self.assertEqual("/anthropic/", anthropic["path"])
        self.assertEqual("https://api.anthropic.com", anthropic["upstream"])
        self.assertEqual("Bearer", anthropic["auth_scheme"])
        self.assertEqual("CRED_PROXY_TOKEN_0", anthropic["token_env"])

    def test_routes_carry_no_token_values_or_host_env_names(self):
        # routes.json lives mode-600 in the staging dir and gets
        # docker cp'd into the sidecar — it must not leak secret values
        # or even the host-side TokenRef name.
        b = _bottle([{"Kind": "github", "TokenRef": "GITHUB_TOKEN"}])
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

        b = _bottle([{"Kind": "github", "TokenRef": "GITHUB_TOKEN"}])
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
