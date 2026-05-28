"""Unit: manifest parsing for `bottle.egress.routes[]` (PRD 0017).

The route shape is new: `host` (required), optional `path_allowlist`,
optional nested `auth: { scheme, token_ref }`. Validation rules per
the PRD: empty `auth: {}` is an error, partial `auth` is an error,
auth omission means unauthenticated."""

import unittest

from bot_bottle.log import Die
from bot_bottle.manifest import EgressRoute, Manifest


def _bottle(routes):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"egress": {"routes": routes}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _provider_bottle(provider, routes):
    return Manifest.from_json_obj({
        "bottles": {
            "dev": {
                "agent_provider": {"template": provider},
                "egress": {"routes": routes},
            }
        },
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


class TestMinimalRoute(unittest.TestCase):
    def test_host_only(self):
        b = _bottle([{"host": "api.example.com"}])
        self.assertEqual(1, len(b.egress.routes))
        r = b.egress.routes[0]
        self.assertEqual("api.example.com", r.Host)
        self.assertEqual((), r.PathAllowlist)
        self.assertEqual("", r.AuthScheme)
        self.assertEqual("", r.TokenRef)

    def test_host_required(self):
        with self.assertRaises(Die):
            _bottle([{}])

    def test_host_must_be_non_empty(self):
        with self.assertRaises(Die):
            _bottle([{"host": ""}])

    def test_unknown_top_level_key_dies(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "wat": "yes"}])


class TestPathAllowlist(unittest.TestCase):
    def test_optional(self):
        b = _bottle([{"host": "x.example"}])
        self.assertEqual((), b.egress.routes[0].PathAllowlist)

    def test_must_be_array(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "path_allowlist": "/x/"}])

    def test_items_must_be_strings(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "path_allowlist": [42]}])

    def test_items_must_be_absolute_paths(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "path_allowlist": ["nope/"]}])

    def test_full_list(self):
        b = _bottle([{
            "host": "github.com",
            "path_allowlist": ["/didericis/", "/users/didericis"],
        }])
        self.assertEqual(
            ("/didericis/", "/users/didericis"),
            b.egress.routes[0].PathAllowlist,
        )


class TestAuth(unittest.TestCase):
    def test_omitted_means_no_auth(self):
        b = _bottle([{"host": "github.com"}])
        r = b.egress.routes[0]
        self.assertEqual("", r.AuthScheme)
        self.assertEqual("", r.TokenRef)

    def test_full_auth(self):
        b = _bottle([{
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
        }])
        r = b.egress.routes[0]
        self.assertEqual("Bearer", r.AuthScheme)
        self.assertEqual("GH_PAT", r.TokenRef)

    def test_empty_auth_block_rejected(self):
        # Per PRD 0017: `auth: {}` is an error, not a synonym for
        # "no auth" — that's what omission is for.
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "auth": {}}])

    def test_missing_scheme_rejected(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "auth": {"token_ref": "T"},
            }])

    def test_missing_token_ref_rejected(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "auth": {"scheme": "Bearer"},
            }])

    def test_unknown_scheme_rejected(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "auth": {"scheme": "Basic", "token_ref": "T"},
            }])

    def test_token_scheme_allowed(self):
        # Gitea quirk: `Authorization: token <pat>` (not Bearer).
        b = _bottle([{
            "host": "git.example",
            "auth": {"scheme": "token", "token_ref": "GITEA_PAT"},
        }])
        self.assertEqual("token", b.egress.routes[0].AuthScheme)

    def test_unknown_auth_key_rejected(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "auth": {"scheme": "Bearer", "token_ref": "T", "extra": "no"},
            }])


class TestRole(unittest.TestCase):
    def test_omitted_means_no_roles(self):
        b = _bottle([{"host": "x.example"}])
        self.assertEqual((), b.egress.routes[0].Role)

    def test_string_normalizes_to_tuple(self):
        b = _bottle([{
            "host": "api.anthropic.com",
            "role": "claude_code_oauth",
            "auth": {"scheme": "Bearer", "token_ref": "T"},
        }])
        self.assertEqual(("claude_code_oauth",),
                         b.egress.routes[0].Role)

    def test_list_supported(self):
        b = _bottle([{
            "host": "api.anthropic.com",
            "role": ["claude_code_oauth"],
            "auth": {"scheme": "Bearer", "token_ref": "T"},
        }])
        self.assertEqual(("claude_code_oauth",),
                         b.egress.routes[0].Role)

    def test_unknown_role_rejected(self):
        # The role enum is locked down — typos shouldn't silently
        # become no-op markers.
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "role": "totally-made-up"}])

    def test_non_string_role_rejected(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "role": 42}])

    def test_list_with_non_string_item_rejected(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example",
                      "role": ["claude_code_oauth", 42]}])

    def test_singleton_claude_code_oauth_enforced(self):
        # Two routes both claiming the role would make "which one
        # drives the placeholder env?" ambiguous.
        with self.assertRaises(Die):
            _bottle([
                {"host": "api.anthropic.com", "role": "claude_code_oauth",
                 "auth": {"scheme": "Bearer", "token_ref": "T1"}},
                {"host": "api2.anthropic.example",
                 "role": "claude_code_oauth",
                 "auth": {"scheme": "Bearer", "token_ref": "T2"}},
            ])

    def test_codex_auth_role_allowed_for_codex_provider(self):
        b = _provider_bottle("codex", [{
            "host": "api.openai.com",
            "role": "codex_auth",
            "auth": {"scheme": "Bearer", "token_ref": "OPENAI_TOKEN"},
        }])
        self.assertEqual(("codex_auth",), b.egress.routes[0].Role)

    def test_claude_role_rejected_for_codex_provider(self):
        with self.assertRaises(Die):
            _provider_bottle("codex", [{
                "host": "api.anthropic.com",
                "role": "claude_code_oauth",
                "auth": {"scheme": "Bearer", "token_ref": "T"},
            }])

    def test_codex_role_rejected_for_default_claude_provider(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "api.openai.com",
                "role": "codex_auth",
                "auth": {"scheme": "Bearer", "token_ref": "T"},
            }])


class TestPipelockPolicy(unittest.TestCase):
    def test_tls_passthrough_route_policy(self):
        b = _bottle([{
            "host": "api.openai.com",
            "pipelock": {"tls_passthrough": True},
        }])
        self.assertTrue(b.egress.routes[0].Pipelock.TlsPassthrough)

    def test_ssrf_ip_allowlist_route_policy(self):
        b = _bottle([{
            "host": "gitea.dideric.is",
            "pipelock": {"ssrf_ip_allowlist": ["192.0.2.10/32"]},
        }])
        self.assertEqual(
            ("192.0.2.10/32",),
            b.egress.routes[0].Pipelock.SsrfIpAllowlist,
        )

    def test_tls_passthrough_defaults_false(self):
        b = _bottle([{"host": "api.openai.com"}])
        self.assertFalse(b.egress.routes[0].Pipelock.TlsPassthrough)
        self.assertEqual((), b.egress.routes[0].Pipelock.SsrfIpAllowlist)

    def test_pipelock_policy_must_be_object(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "pipelock": True}])

    def test_tls_passthrough_must_be_bool(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "pipelock": {"tls_passthrough": "yes"},
            }])

    def test_ssrf_ip_allowlist_must_be_array(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "pipelock": {"ssrf_ip_allowlist": "192.0.2.10/32"},
            }])

    def test_ssrf_ip_allowlist_items_must_be_cidr_or_ip(self):
        with self.assertRaises(Die):
            _bottle([{
                "host": "x.example",
                "pipelock": {"ssrf_ip_allowlist": ["not-an-ip"]},
            }])

    def test_unknown_pipelock_key_rejected(self):
        with self.assertRaises(Die):
            _bottle([{"host": "x.example", "pipelock": {"wat": True}}])


class TestRouteValidation(unittest.TestCase):
    def test_duplicate_hosts_rejected(self):
        # Routes match by exact host; duplicates leave the choice
        # ambiguous, so we reject them up front rather than picking
        # the first/last silently.
        with self.assertRaises(Die):
            _bottle([
                {"host": "github.com"},
                {"host": "github.com", "path_allowlist": ["/x/"]},
            ])

    def test_duplicate_host_case_insensitive(self):
        with self.assertRaises(Die):
            _bottle([
                {"host": "GitHub.com"},
                {"host": "github.com"},
            ])

    def test_empty_routes_allowed(self):
        b = _bottle([])
        self.assertEqual((), b.egress.routes)

    def test_no_egress_block_means_empty(self):
        # The bottle dataclass defaults to an empty EgressConfig.
        b = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        self.assertEqual((), b.egress.routes)


class TestConfigShape(unittest.TestCase):
    def test_unknown_egress_key_rejected(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj({
                "bottles": {"dev": {"egress": {"wat": []}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
