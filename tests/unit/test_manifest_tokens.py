"""Unit: bottle.cred_proxy.routes manifest parsing + validation (PRD 0010)."""

import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Manifest


def _manifest(routes, git=None):
    bottle: dict[str, object] = {"cred_proxy": {"routes": routes}}
    if git is not None:
        bottle["git"] = git
    return {
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestCredProxyRouteParsing(unittest.TestCase):
    def test_parses_minimal_route(self):
        m = Manifest.from_json_obj(_manifest([
            {"path": "/anthropic/",
             "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer",
             "token_ref": "CLAUDE_BOTTLE_OAUTH_TOKEN"},
        ]))
        routes = m.bottles["dev"].cred_proxy.routes
        self.assertEqual(1, len(routes))
        r = routes[0]
        self.assertEqual("/anthropic/", r.Path)
        self.assertEqual("https://api.anthropic.com", r.Upstream)
        self.assertEqual("Bearer", r.AuthScheme)
        self.assertEqual("CLAUDE_BOTTLE_OAUTH_TOKEN", r.TokenRef)
        self.assertEqual((), r.Role)
        self.assertEqual("api.anthropic.com", r.UpstreamHost)

    def test_role_string_normalizes_to_tuple(self):
        m = Manifest.from_json_obj(_manifest([
            {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer", "token_ref": "T",
             "role": "anthropic-base-url"},
        ]))
        self.assertEqual(("anthropic-base-url",),
                         m.bottles["dev"].cred_proxy.routes[0].Role)

    def test_role_list_supported(self):
        m = Manifest.from_json_obj(_manifest([
            {"path": "/gitea/x/", "upstream": "https://gitea.example.com",
             "auth_scheme": "token", "token_ref": "T",
             "role": ["git-insteadof", "tea-login"]},
        ]))
        self.assertEqual(("git-insteadof", "tea-login"),
                         m.bottles["dev"].cred_proxy.routes[0].Role)

    def test_upstream_host_extracted(self):
        m = Manifest.from_json_obj(_manifest([
            {"path": "/gitea/x/", "upstream": "https://gitea.dideric.is:30443",
             "auth_scheme": "token", "token_ref": "T"},
        ]))
        self.assertEqual("gitea.dideric.is",
                         m.bottles["dev"].cred_proxy.routes[0].UpstreamHost)


class TestCredProxyRouteValidation(unittest.TestCase):
    def _route(self, **overrides):
        base = {
            "path": "/x/",
            "upstream": "https://example.com",
            "auth_scheme": "Bearer",
            "token_ref": "TOK",
        }
        base.update(overrides)
        return base

    def test_missing_path_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(path=None)]))

    def test_path_without_trailing_slash_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(path="/no-slash")]))

    def test_path_without_leading_slash_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(path="no-slash/")]))

    def test_missing_upstream_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(upstream=None)]))

    def test_non_https_upstream_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(upstream="http://x.example")]))

    def test_unknown_auth_scheme_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(auth_scheme="Basic")]))

    def test_missing_token_ref_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(token_ref=None)]))

    def test_unknown_role_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([self._route(role="something-made-up")]))


class TestCredProxyCrossValidation(unittest.TestCase):
    def test_duplicate_path_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"path": "/x/", "upstream": "https://a.example",
                 "auth_scheme": "Bearer", "token_ref": "T1"},
                {"path": "/x/", "upstream": "https://b.example",
                 "auth_scheme": "Bearer", "token_ref": "T2"},
            ]))

    def test_two_routes_same_anthropic_role_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
                 "auth_scheme": "Bearer", "token_ref": "A1",
                 "role": "anthropic-base-url"},
                {"path": "/anthropic-2/", "upstream": "https://api.anthropic.com",
                 "auth_scheme": "Bearer", "token_ref": "A2",
                 "role": "anthropic-base-url"},
            ]))

    def test_multiple_git_insteadof_ok(self):
        # git-insteadof is not a singleton role — each route can
        # independently rewrite its own host.
        m = Manifest.from_json_obj(_manifest([
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GH",
             "role": "git-insteadof"},
            {"path": "/gitea/x/", "upstream": "https://gitea.example.com",
             "auth_scheme": "token", "token_ref": "GT",
             "role": "git-insteadof"},
        ]))
        self.assertEqual(2, len(m.bottles["dev"].cred_proxy.routes))


class TestLegacyTokensField(unittest.TestCase):
    def test_legacy_tokens_field_dies_with_hint(self):
        # The PRD-iteration shape ({"tokens": [{Kind: ...}]}) was
        # replaced by cred_proxy.routes; old manifests must fail
        # loudly with a pointer.
        with self.assertRaises(Die):
            Manifest.from_json_obj({
                "bottles": {"dev": {"tokens": [
                    {"Kind": "anthropic", "TokenRef": "T"},
                ]}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })


class TestEmptyCredProxy(unittest.TestCase):
    def test_no_cred_proxy_field_yields_empty_routes(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].cred_proxy.routes)

    def test_routes_array_type_required(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj({
                "bottles": {"dev": {"cred_proxy": {"routes": "not-a-list"}}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
