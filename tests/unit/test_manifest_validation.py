"""Unit: manifest + manifest_agent validation error/edge branches
(coverage ratchet, ADR 0004).

Drives ManifestBottle / ManifestAgentProvider / ManifestAgent / the
provider-settings parser and the eager ManifestIndex lookup methods
through their rejection and edge paths."""

from __future__ import annotations

import unittest

from bot_bottle.manifest import ManifestBottle, ManifestIndex
from bot_bottle.manifest_agent import (
    ManifestAgent,
    ManifestAgentProvider,
    _parse_provider_settings,
)
from bot_bottle.manifest_util import ManifestError


def _idx(obj: dict[str, object]) -> ManifestIndex:
    return ManifestIndex.from_json_obj(obj)


# ---------------------------------------------------------------------------
# ManifestBottle.from_dict
# ---------------------------------------------------------------------------


class TestBottleValidation(unittest.TestCase):
    def test_unknown_key(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBottle.from_dict("b", {"bogus": 1})

    def test_env_value_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBottle.from_dict("b", {"env": {"X": 5}})

    def test_supervise_not_bool(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBottle.from_dict("b", {"supervise": "yes"})

    def test_removed_runtime_field(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBottle.from_dict("b", {"runtime": "runsc"})

    def test_valid_minimal(self) -> None:
        b = ManifestBottle.from_dict("b", {"supervise": False, "env": {"X": "1"}})
        self.assertFalse(b.supervise)
        self.assertEqual({"X": "1"}, dict(b.env))


# ---------------------------------------------------------------------------
# ManifestAgentProvider.from_dict
# ---------------------------------------------------------------------------


class TestAgentProviderValidation(unittest.TestCase):
    def test_unknown_key(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"bogus": 1})

    def test_empty_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"template": ""})

    def test_dockerfile_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"dockerfile": 5})

    def test_auth_token_unknown_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"auth_token": "x", "template": "weird"})

    def test_auth_token_non_claude_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"auth_token": "x", "template": "codex"})

    def test_forward_creds_unknown_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"forward_host_credentials": True, "template": "weird"}
            )

    def test_forward_creds_non_codex_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"forward_host_credentials": True, "template": "claude"}
            )

    def test_valid_claude_auth_token(self) -> None:
        p = ManifestAgentProvider.from_dict("b", {"template": "claude", "auth_token": "T"})
        self.assertEqual("T", p.auth_token)


# ---------------------------------------------------------------------------
# _parse_provider_settings
# ---------------------------------------------------------------------------


class TestProviderSettings(unittest.TestCase):
    def test_unknown_template_passes_settings_through(self) -> None:
        out = _parse_provider_settings("b", "weird", {"anything": 1})
        self.assertEqual({"anything": 1}, out)

    def test_startup_args_not_list(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "claude", {"startup_args": "x"})

    def test_startup_args_empty_item(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "claude", {"startup_args": [""]})

    def test_pi_string_field_empty(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "pi", {"provider": ""})

    def test_pi_max_tokens_field_invalid(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "pi", {"max_tokens_field": "bogus"})

    def test_pi_api_key_and_env_conflict(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "pi", {"api_key": "k", "api_key_env": "E"})

    def test_pi_models_item_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "pi", {"models": [5]})

    def test_pi_bool_field_not_bool(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "pi", {"supports_developer_role": "yes"})

    def test_pi_context_window_not_positive(self) -> None:
        with self.assertRaises(ManifestError):
            _parse_provider_settings("b", "pi", {"context_window": -1})

    def test_pi_valid_settings(self) -> None:
        out = _parse_provider_settings(
            "b", "pi",
            {"provider": "openai", "models": ["gpt"], "context_window": 8000},
        )
        self.assertEqual("openai", out["provider"])


# ---------------------------------------------------------------------------
# ManifestAgent.from_dict
# ---------------------------------------------------------------------------


class TestAgentValidation(unittest.TestCase):
    def test_bottle_empty_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"bottle": ""}, set())

    def test_bottle_undefined(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"bottle": "x"}, set())

    def test_skills_not_list(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"skills": "x"}, set())

    def test_skill_item_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"skills": [5]}, set())

    def test_skill_name_rejects_shell_metacharacters(self) -> None:
        # Skill names become host/guest path segments interpolated into
        # provisioning shell commands; anything outside kebab-case is
        # rejected at load so it can never reach a `bottle.exec` string.
        for bad in ("foo; rm -rf /", "../escape", "foo bar", "Foo", "-leading"):
            with self.assertRaises(ManifestError):
                ManifestAgent.from_dict("a", {"skills": [bad]}, set())

    def test_skill_name_accepts_kebab_case(self) -> None:
        agent = ManifestAgent.from_dict(
            "a", {"skills": ["init-entry", "quality-eval", "skill0"]}, set()
        )
        self.assertEqual(
            agent.skills, ("init-entry", "quality-eval", "skill0")
        )

    def test_prompt_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"prompt": 5}, set())

    def test_git_gate_repos_rejected_at_agent_level(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"git-gate": {"repos": {}}}, set())

    def test_git_gate_empty_is_allowed(self) -> None:
        agent = ManifestAgent.from_dict("a", {"git-gate": {}}, set())
        self.assertTrue(agent.git_user.is_empty())


# ---------------------------------------------------------------------------
# Eager ManifestIndex lookup methods
# ---------------------------------------------------------------------------


class TestEagerIndexLookups(unittest.TestCase):
    def _idx(self) -> ManifestIndex:
        return _idx({
            "bottles": {"b": {"git-gate": {"user": {"name": "Bot", "email": "b@x"}}}},
            "agents": {"a": {"bottle": "b"}},
        })

    def test_unknown_bottle_section_is_empty(self) -> None:
        # no "bottles" key -> _section_dict(None) path
        idx = _idx({"agents": {"a": {}}})
        self.assertEqual(["a"], idx.all_agent_names)

    def test_load_unknown_agent_raises(self) -> None:
        with self.assertRaises(ManifestError):
            self._idx().load_for_agent("nope")

    def test_has_agent(self) -> None:
        idx = self._idx()
        self.assertTrue(idx.has_agent("a"))
        self.assertFalse(idx.has_agent("nope"))

    def test_require_agent_known_and_unknown(self) -> None:
        idx = self._idx()
        idx.require_agent("a")  # no raise
        with self.assertRaises(ManifestError):
            idx.require_agent("nope")

    def test_git_identity_summary(self) -> None:
        m = self._idx().load_for_agent("a")
        summary = m.git_identity_summary()
        assert summary is not None
        self.assertIn("name=Bot", summary)
        self.assertIn("email=b@x", summary)

    def test_git_identity_summary_none_when_empty(self) -> None:
        m = _idx({"bottles": {"b": {}}, "agents": {"a": {"bottle": "b"}}}).load_for_agent("a")
        self.assertIsNone(m.git_identity_summary())


if __name__ == "__main__":
    unittest.main()
