"""Unit: manifest parsing for `bottle.egress.routes[]` (PRD 0017).

The route shape is new: `host` (required), optional `path_allowlist`,
optional nested `auth: { scheme, token_ref }`. Validation rules per
the PRD: empty `auth: {}` is an error, partial `auth` is an error,
auth omission means unauthenticated."""

import unittest

from bot_bottle.manifest import ManifestError, Manifest


def _bottle(routes):  # type: ignore
    return Manifest.from_json_obj({
        "bottles": {"dev": {"egress": {"routes": routes}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _provider_bottle(provider, routes):  # type: ignore
    return Manifest.from_json_obj({
        "bottles": {
            "dev": {
                "agent_provider": {"template": provider},
                "egress": {"routes": routes},
            }
        },
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _provider_config_bottle(agent_provider):  # type: ignore
    return Manifest.from_json_obj({
        "bottles": {"dev": {"agent_provider": agent_provider}},
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
        with self.assertRaises(ManifestError):
            _bottle([{}])

    def test_host_must_be_non_empty(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": ""}])

    def test_unknown_top_level_key_dies(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "wat": "yes"}])


class TestAgentProviderHostCredentials(unittest.TestCase):
    def test_forward_host_credentials_defaults_false(self):
        b = _provider_config_bottle({"template": "codex"})
        self.assertFalse(b.agent_provider.forward_host_credentials)

    def test_forward_host_credentials_allowed_for_codex(self):
        b = _provider_config_bottle({
            "template": "codex",
            "forward_host_credentials": True,
        })
        self.assertTrue(b.agent_provider.forward_host_credentials)

    def test_forward_host_credentials_must_be_boolean(self):
        with self.assertRaises(ManifestError):
            _provider_config_bottle({
                "template": "codex",
                "forward_host_credentials": "yes",
            })

    def test_forward_host_credentials_rejected_for_claude(self):
        with self.assertRaises(ManifestError):
            _provider_config_bottle({
                "template": "claude",
                "forward_host_credentials": True,
            })

    def test_auth_token_defaults_empty(self):
        b = _provider_config_bottle({"template": "claude"})
        self.assertEqual("", b.agent_provider.auth_token)

    def test_auth_token_allowed_for_claude(self):
        b = _provider_config_bottle({
            "template": "claude",
            "auth_token": "BOT_BOTTLE_CLAUDE_OAUTH_TOKEN",
        })
        self.assertEqual("BOT_BOTTLE_CLAUDE_OAUTH_TOKEN", b.agent_provider.auth_token)

    def test_auth_token_must_be_string(self):
        with self.assertRaises(ManifestError):
            _provider_config_bottle({
                "template": "claude",
                "auth_token": 42,
            })

    def test_auth_token_rejected_for_codex(self):
        with self.assertRaises(ManifestError):
            _provider_config_bottle({
                "template": "codex",
                "auth_token": "SOME_TOKEN",
            })


class TestPathAllowlist(unittest.TestCase):
    def test_optional(self):
        b = _bottle([{"host": "x.example"}])
        self.assertEqual((), b.egress.routes[0].PathAllowlist)

    def test_must_be_array(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "path_allowlist": "/x/"}])

    def test_items_must_be_strings(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "path_allowlist": [42]}])

    def test_items_must_be_absolute_paths(self):
        with self.assertRaises(ManifestError):
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
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "auth": {}}])

    def test_missing_scheme_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{
                "host": "x.example",
                "auth": {"token_ref": "T"},
            }])

    def test_missing_token_ref_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{
                "host": "x.example",
                "auth": {"scheme": "Bearer"},
            }])

    def test_unknown_scheme_rejected(self):
        with self.assertRaises(ManifestError):
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
        with self.assertRaises(ManifestError):
            _bottle([{
                "host": "x.example",
                "auth": {"scheme": "Bearer", "token_ref": "T", "extra": "no"},
            }])


class TestRole(unittest.TestCase):
    def test_omitted_means_no_roles(self):
        b = _bottle([{"host": "x.example"}])
        self.assertEqual((), b.egress.routes[0].Role)

    def test_any_role_rejected(self):
        # All former roles removed; the field is reserved for future use.
        for role in ("claude_code_oauth", "codex_auth", "totally-made-up"):
            with self.subTest(role=role):
                with self.assertRaises(ManifestError):
                    _bottle([{"host": "x.example", "role": role}])

    def test_non_string_role_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "role": 42}])

    def test_list_with_non_string_item_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "role": ["x", 42]}])


class TestPipelockKeyRejected(unittest.TestCase):
    def test_pipelock_key_rejected_as_unknown(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "pipelock": {"tls_passthrough": True}}])


class TestRouteValidation(unittest.TestCase):
    def test_duplicate_hosts_rejected(self):
        # Routes match by exact host; duplicates leave the choice
        # ambiguous, so we reject them up front rather than picking
        # the first/last silently.
        with self.assertRaises(ManifestError):
            _bottle([
                {"host": "github.com"},
                {"host": "github.com", "path_allowlist": ["/x/"]},
            ])

    def test_duplicate_host_case_insensitive(self):
        with self.assertRaises(ManifestError):
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
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"egress": {"wat": []}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
