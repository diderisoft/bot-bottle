"""Unit: manifest parsing for `bottle.egress.routes[]` (PRD 0017, PRD 0053).

The route shape uses Gateway API HTTPRoute match vocabulary:
`host` (required), optional `matches` (paths/methods/headers),
optional nested `auth: { scheme, token_ref }`, optional `dlp`.
Validation rules per PRD 0017/0053: empty `auth: {}` is an error,
partial `auth` is an error, auth omission means unauthenticated."""

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
        self.assertEqual((), r.Matches)
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


class TestMatches(unittest.TestCase):
    def test_optional(self):
        b = _bottle([{"host": "x.example"}])
        self.assertEqual((), b.egress.routes[0].Matches)

    def test_must_be_array(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "matches": "nope"}])

    def test_path_prefix_default(self):
        b = _bottle([{"host": "x.example", "matches": [
            {"paths": [{"value": "/api/"}]}
        ]}])
        m = b.egress.routes[0].Matches[0]
        self.assertEqual(1, len(m.Paths))
        self.assertEqual("prefix", m.Paths[0].Type)
        self.assertEqual("/api/", m.Paths[0].Value)

    def test_path_exact(self):
        b = _bottle([{"host": "x.example", "matches": [
            {"paths": [{"type": "exact", "value": "/health"}]}
        ]}])
        self.assertEqual("exact", b.egress.routes[0].Matches[0].Paths[0].Type)

    def test_path_regex(self):
        b = _bottle([{"host": "x.example", "matches": [
            {"paths": [{"type": "regex", "value": "^/api/v[0-9]+/"}]}
        ]}])
        self.assertEqual("regex", b.egress.routes[0].Matches[0].Paths[0].Type)

    def test_path_invalid_regex_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "matches": [
                {"paths": [{"type": "regex", "value": "[unclosed"}]}
            ]}])

    def test_path_must_start_with_slash_for_prefix(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "matches": [
                {"paths": [{"value": "nope"}]}
            ]}])

    def test_methods_normalised_to_uppercase(self):
        b = _bottle([{"host": "x.example", "matches": [
            {"methods": ["get", "Post"]}
        ]}])
        self.assertEqual(("GET", "POST"), b.egress.routes[0].Matches[0].Methods)

    def test_invalid_method_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "matches": [
                {"methods": ["INVALID"]}
            ]}])

    def test_headers_exact(self):
        b = _bottle([{"host": "x.example", "matches": [
            {"headers": [{"name": "content-type", "value": "application/json"}]}
        ]}])
        h = b.egress.routes[0].Matches[0].Headers[0]
        self.assertEqual("content-type", h.Name)
        self.assertEqual("application/json", h.Value)
        self.assertEqual("exact", h.Type)

    def test_headers_regex(self):
        b = _bottle([{"host": "x.example", "matches": [
            {"headers": [{"name": "accept", "value": "text/.*", "type": "regex"}]}
        ]}])
        self.assertEqual("regex", b.egress.routes[0].Matches[0].Headers[0].Type)

    def test_unknown_match_entry_key_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "matches": [
                {"paths": [{"value": "/x/"}], "bogus": True}
            ]}])


class TestDlp(unittest.TestCase):
    def test_omitted_means_all_enabled(self):
        b = _bottle([{"host": "x.example"}])
        r = b.egress.routes[0]
        self.assertIsNone(r.OutboundDetectors)
        self.assertIsNone(r.InboundDetectors)

    def test_false_means_disabled(self):
        b = _bottle([{"host": "x.example", "dlp": {
            "outbound_detectors": False,
            "inbound_detectors": False,
        }}])
        r = b.egress.routes[0]
        self.assertEqual((), r.OutboundDetectors)
        self.assertEqual((), r.InboundDetectors)

    def test_named_detectors(self):
        b = _bottle([{"host": "x.example", "dlp": {
            "outbound_detectors": ["token_patterns"],
            "inbound_detectors": ["naive_injection_detection"],
        }}])
        r = b.egress.routes[0]
        self.assertEqual(("token_patterns",), r.OutboundDetectors)
        self.assertEqual(("naive_injection_detection",), r.InboundDetectors)

    def test_unknown_detector_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "dlp": {
                "outbound_detectors": ["nonexistent"],
            }}])

    def test_unknown_dlp_key_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "dlp": {
                "bogus": True,
            }}])


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
        with self.assertRaises(ManifestError):
            _bottle([
                {"host": "github.com"},
                {"host": "github.com", "matches": [
                    {"paths": [{"value": "/x/"}]}
                ]},
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

    def test_log_defaults_zero(self):
        b = _bottle([])
        self.assertEqual(0, b.egress.Log)

    def test_log_level_1_accepted(self):
        b = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"log": 1, "routes": []}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        self.assertEqual(1, b.egress.Log)

    def test_log_level_2_accepted(self):
        b = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"log": 2, "routes": []}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        self.assertEqual(2, b.egress.Log)

    def test_log_invalid_level_rejected(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"egress": {"log": 3}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })

    def test_log_bool_rejected(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"egress": {"log": True}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })

    def test_log_string_rejected(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"egress": {"log": "full"}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
