"""Unit: manifest parsing for `bottle.egress.routes[]` (PRD 0017, PRD 0053).

The route shape uses Gateway API HTTPRoute match vocabulary:
`host` (required), optional `matches` (paths/methods/headers),
optional nested `auth: { scheme, token_ref }`, optional `dlp`,
optional `git: { fetch: true }`.
Validation rules per PRD 0017/0053: empty `auth: {}` is an error,
partial `auth` is an error, auth omission means unauthenticated."""

import unittest

from bot_bottle.manifest import ManifestError, ManifestIndex


def _inspect_routes(routes):  # type: ignore
    out = []
    for route in routes:
        route = dict(route)
        if "dlp" in route:
            dlp = route.pop("dlp")
            if dlp is False:
                route["inspect"] = False
                out.append(route)
                continue
            route["inspect"] = dlp
        controls = ("matches", "auth", "git", "preserve_auth")
        moved = {key: route.pop(key) for key in controls if key in route}
        if moved:
            inspected = dict(route.get("inspect", {}))
            inspected.update(moved)
            route["inspect"] = inspected
        out.append(route)
    return out


def _bottle(routes):  # type: ignore
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": {"egress": {"routes": _inspect_routes(routes)}}},
        "agents": {"demo": {"skills": [], "prompt": ""}},
    }).bottles["dev"]


def _provider_config_agent(agent_provider):  # type: ignore
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {"demo": {"skills": [], "prompt": "",
                            "agent_provider": agent_provider}},
    }).agents["demo"]


class TestAgentOwnsProvider(unittest.TestCase):
    def test_the_default_is_claude(self):
        self.assertEqual("claude", _provider_config_agent({}).agent_provider.harness)

    def test_the_string_shorthand_is_the_harness(self):
        self.assertEqual(_provider_config_agent({"harness": "pi"}).agent_provider,
                         _provider_config_agent("pi").agent_provider)

    def test_template_is_an_unknown_key(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({"template": "pi"})

    def test_a_bottle_declaring_it_is_told_to_move_it(self):
        with self.assertRaises(ManifestError) as e:
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"agent_provider": {"harness": "pi"}}},
                "agents": {"demo": {"skills": [], "prompt": ""}},
            })
        self.assertIn("belongs to the agent", str(e.exception))

    def test_one_bottle_serves_two_harnesses(self):
        index = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [{"host": "api.example.com"}]}}},
            "agents": {
                "a": {"skills": [], "prompt": "",
                      "agent_provider": {"harness": "claude", "auth_token": "TOK"}},
                "b": {"skills": [], "prompt": "",
                      "agent_provider": {"harness": "codex",
                                         "forward_host_credentials": True}},
            },
        })
        a, b = index.load_for_agent("a"), index.load_for_agent("b")
        self.assertEqual(a.bottle, b.bottle)
        self.assertEqual(("claude", "codex"),
                         (a.agent.agent_provider.harness, b.agent.agent_provider.harness))


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
        b = _provider_config_agent({"harness": "codex"})
        self.assertFalse(b.agent_provider.forward_host_credentials)

    def test_forward_host_credentials_allowed_for_codex(self):
        b = _provider_config_agent({
            "harness": "codex",
            "forward_host_credentials": True,
        })
        self.assertTrue(b.agent_provider.forward_host_credentials)

    def test_forward_host_credentials_must_be_boolean(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "codex",
                "forward_host_credentials": "yes",
            })

    def test_forward_host_credentials_allowed_for_claude(self):
        b = _provider_config_agent({
            "harness": "claude",
            "forward_host_credentials": True,
        })
        self.assertTrue(b.agent_provider.forward_host_credentials)

    def test_forward_host_credentials_and_auth_token_rejected_together(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "claude",
                "forward_host_credentials": True,
                "auth_token": "SOME_TOKEN",
            })

    def test_auth_token_defaults_empty(self):
        b = _provider_config_agent({"harness": "claude"})
        self.assertEqual("", b.agent_provider.auth_token)

    def test_auth_token_allowed_for_claude(self):
        b = _provider_config_agent({
            "harness": "claude",
            "auth_token": "BOT_BOTTLE_CLAUDE_OAUTH_TOKEN",
        })
        self.assertEqual("BOT_BOTTLE_CLAUDE_OAUTH_TOKEN", b.agent_provider.auth_token)

    def test_auth_token_must_be_string(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "claude",
                "auth_token": 42,
            })

    def test_auth_token_rejected_for_codex(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "codex",
                "auth_token": "SOME_TOKEN",
            })

    def test_settings_allowed_for_pi(self):
        b = _provider_config_agent({
            "harness": "pi",
            "settings": {
                "provider": "ollama",
                "base_url": "http://ollama:11434/v1",
                "api": "openai-completions",
                "api_key": "ollama",
                "models": ["qwen2.5-coder:7b"],
                "context_window": 65536,
                "max_tokens_field": "max_tokens",
                "max_tokens": 12000,
                "supports_developer_role": False,
                "supports_reasoning_effort": False,
            },
        })
        self.assertEqual(
            {
                "provider": "ollama",
                "base_url": "http://ollama:11434/v1",
                "api": "openai-completions",
                "api_key": "ollama",
                "models": ["qwen2.5-coder:7b"],
                "context_window": 65536,
                "max_tokens_field": "max_tokens",
                "max_tokens": 12000,
                "supports_developer_role": False,
                "supports_reasoning_effort": False,
            },
            b.agent_provider.settings,
        )

    def test_settings_allowed_for_pi_api_key_env(self):
        b = _provider_config_agent({
            "harness": "pi",
            "settings": {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api": "openai-completions",
                "api_key_env": "OPENROUTER_API_KEY",
                "models": ["google/gemma-4-26b-a4b-it:free"],
            },
        })
        self.assertEqual("OPENROUTER_API_KEY", b.agent_provider.settings["api_key_env"])

    def test_settings_rejects_api_key_and_api_key_env_together(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "pi",
                "settings": {
                    "api_key": "literal",
                    "api_key_env": "OPENROUTER_API_KEY",
                },
            })

    def test_startup_args_allowed_for_claude(self):
        b = _provider_config_agent({
            "harness": "claude",
            "settings": {"startup_args": ["--model", "opus"]},
        })
        self.assertEqual(
            {"startup_args": ["--model", "opus"]},
            b.agent_provider.settings,
        )

    def test_startup_args_allowed_for_codex(self):
        b = _provider_config_agent({
            "harness": "codex",
            "settings": {"startup_args": ["--model", "gpt-5-codex"]},
        })
        self.assertEqual(
            {"startup_args": ["--model", "gpt-5-codex"]},
            b.agent_provider.settings,
        )

    def test_provider_specific_settings_still_rejected_for_claude(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "claude",
                "settings": {"models": ["qwen2.5-coder:7b"]},
            })

    def test_startup_args_must_be_string_array(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "codex",
                "settings": {"startup_args": ["--model", 42]},
            })

    def test_settings_models_must_be_non_empty_string_array(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "pi",
                "settings": {"models": []},
            })

    def test_settings_boolean_flags_must_be_boolean(self):
        with self.assertRaises(ManifestError):
            _provider_config_agent({
                "harness": "pi",
                "settings": {"supports_developer_role": "no"},
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

    def test_inspect_false_sets_passthrough(self):
        b = _bottle([{"host": "x.example", "inspect": False}])
        r = b.egress.routes[0]
        self.assertFalse(r.Inspect)

    def test_inspect_defaults_true(self):
        b = _bottle([{"host": "x.example"}])
        self.assertTrue(b.egress.routes[0].Inspect)

    def test_inspect_not_dict_or_false_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "inspect": "nope"}])

    def test_outbound_on_match_omitted_is_empty(self):
        b = _bottle([{"host": "x.example"}])
        self.assertEqual("", b.egress.routes[0].OutboundOnMatch)

    def test_outbound_on_match_accepts_policies(self):
        for policy in ("block", "redact", "supervise"):
            with self.subTest(policy=policy):
                b = _bottle([{"host": "x.example", "dlp": {
                    "outbound_on_match": policy,
                }}])
                self.assertEqual(policy, b.egress.routes[0].OutboundOnMatch)

    def test_outbound_on_match_rejects_unknown_value(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "x.example", "dlp": {
                "outbound_on_match": "allow",
            }}])


class TestGitPolicy(unittest.TestCase):
    def test_omitted_means_https_git_fetch_disabled(self):
        b = _bottle([{"host": "github.com"}])
        self.assertFalse(b.egress.routes[0].GitFetch)

    def test_fetch_true_allowed(self):
        b = _bottle([{"host": "github.com", "git": {"fetch": True}}])
        self.assertTrue(b.egress.routes[0].GitFetch)

    def test_fetch_false_allowed(self):
        b = _bottle([{"host": "github.com", "git": {"fetch": False}}])
        self.assertFalse(b.egress.routes[0].GitFetch)

    def test_git_must_be_object(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "github.com", "git": True}])

    def test_fetch_must_be_boolean(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "github.com", "git": {"fetch": "yes"}}])

    def test_unknown_git_key_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "github.com", "git": {"push": True}}])


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


class TestPreserveAuth(unittest.TestCase):
    def test_omitted_defaults_false(self):
        b = _bottle([{"host": "registry-1.docker.io"}])
        self.assertFalse(b.egress.routes[0].PreserveAuth)

    def test_true_accepted(self):
        b = _bottle([{"host": "registry-1.docker.io", "preserve_auth": True}])
        self.assertTrue(b.egress.routes[0].PreserveAuth)

    def test_false_accepted(self):
        b = _bottle([{"host": "registry-1.docker.io", "preserve_auth": False}])
        self.assertFalse(b.egress.routes[0].PreserveAuth)

    def test_non_bool_rejected(self):
        with self.assertRaises(ManifestError):
            _bottle([{"host": "registry-1.docker.io", "preserve_auth": "yes"}])


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
        b = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": ""}},
        }).bottles["dev"]
        self.assertEqual((), b.egress.routes)


class TestConfigShape(unittest.TestCase):
    def test_unknown_egress_key_rejected(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"egress": {"wat": []}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })

    def test_log_defaults_zero(self):
        b = _bottle([])
        self.assertEqual(0, b.egress.Log)

    def test_log_level_1_accepted(self):
        b = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"egress": {"log": 1, "routes": []}}},
            "agents": {"demo": {"skills": [], "prompt": ""}},
        }).bottles["dev"]
        self.assertEqual(1, b.egress.Log)

    def test_log_level_2_accepted(self):
        b = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"egress": {"log": 2, "routes": []}}},
            "agents": {"demo": {"skills": [], "prompt": ""}},
        }).bottles["dev"]
        self.assertEqual(2, b.egress.Log)

    def test_log_invalid_level_rejected(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"egress": {"log": 3}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })

    def test_log_bool_rejected(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"egress": {"log": True}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })

    def test_log_string_rejected(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"egress": {"log": "full"}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                     "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
