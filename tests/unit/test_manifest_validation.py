"""Unit: manifest + manifest_agent validation error/edge branches
(coverage ratchet, ADR 0004).

Drives ManifestBottle / ManifestAgentProvider / ManifestAgent / the
provider-settings parser and the eager ManifestIndex lookup methods
through their rejection and edge paths."""

from __future__ import annotations

import unittest

from bot_bottle.env import resolve_env
from bot_bottle.errors import MissingEnvVarError
from bot_bottle.manifest import Manifest, ManifestBottle, ManifestIndex
from bot_bottle.manifest.agent import (
    ManifestAgent,
    ManifestAgentProvider,
    _parse_provider_settings,
)
from bot_bottle.manifest.util import ManifestError
from bot_bottle.secret_source import MappingSecrets


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

    def test_nested_containers_not_bool(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestBottle.from_dict("b", {"nested_containers": "yes"})

    def test_nested_containers_defaults_off(self) -> None:
        self.assertFalse(ManifestBottle.from_dict("b", {}).nested_containers)
        self.assertTrue(
            ManifestBottle.from_dict("b", {"nested_containers": True})
            .nested_containers
        )

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
            ManifestAgentProvider.from_dict("b", {"harness": ""})

    def test_dockerfile_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"dockerfile": 5})

    def test_auth_token_unknown_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"auth_token": "x", "harness": "weird"})

    def test_auth_token_non_claude_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {"auth_token": "x", "harness": "codex"})

    def test_forward_creds_unknown_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"forward_host_credentials": True, "harness": "weird"}
            )

    def test_forward_creds_pi_template_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"forward_host_credentials": True, "harness": "pi"}
            )

    def test_forward_creds_claude_allowed(self) -> None:
        p = ManifestAgentProvider.from_dict(
            "b", {"forward_host_credentials": True, "harness": "claude"}
        )
        self.assertTrue(p.forward_host_credentials)

    def test_forward_creds_and_auth_token_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"forward_host_credentials": True, "auth_token": "T", "harness": "claude"}
            )

    def test_image_overlay_parses(self) -> None:
        p = ManifestAgentProvider.from_dict("b", {
            "harness": "claude",
            "overlay": {
                "harness_version": "2.1.180",
                "apt": ["jq", "postgresql-client"],
                "files": ["extra/settings.json"],
            },
        })
        self.assertEqual(p.overlay.harness_version, "2.1.180")
        self.assertEqual(p.overlay.apt, ("jq", "postgresql-client"))
        self.assertEqual(p.overlay.files, ("extra/settings.json",))
        self.assertFalse(p.overlay.is_empty())

    def test_image_overlay_harness_version_accepts_prerelease(self) -> None:
        p = ManifestAgentProvider.from_dict("b", {
            "harness": "codex",
            "overlay": {"harness_version": "0.146.0-alpha.1"},
        })
        self.assertEqual(p.overlay.harness_version, "0.146.0-alpha.1")

    def test_image_overlay_harness_version_must_be_exact(self) -> None:
        for bad in ("latest", "2.1", "^2.1.180", "2.1.180; rm -rf /", "v2.1.180"):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict("b", {
                    "harness": "claude",
                    "overlay": {"harness_version": bad},
                })

    def test_image_overlay_dockerfile_parses(self) -> None:
        p = ManifestAgentProvider.from_dict("b", {
            "harness": "claude",
            "overlay": {"dockerfile": "overlay/Dockerfile"},
        })
        self.assertEqual(p.overlay.dockerfile, "overlay/Dockerfile")
        self.assertFalse(p.overlay.is_empty())
        self.assertFalse(p.overlay.dockerfile_is_inline)

    def test_image_overlay_dockerfile_inline_parses(self) -> None:
        content = "FROM ${BASE}\nRUN apt-get install -y ripgrep\n"
        p = ManifestAgentProvider.from_dict("b", {
            "harness": "claude",
            "overlay": {"dockerfile": content},
        })
        self.assertEqual(p.overlay.dockerfile, content)
        self.assertTrue(p.overlay.dockerfile_is_inline)
        self.assertFalse(p.overlay.is_empty())

    def test_image_overlay_dockerfile_inline_is_exclusive(self) -> None:
        content = "FROM ${BASE}\nRUN true\n"
        for extra in ({"apt": ["jq"]}, {"files": ["f"]}, {"harness_version": "2.1.180"}):
            with self.subTest(extra=extra), self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict("b", {
                    "harness": "claude",
                    "overlay": {"dockerfile": content, **extra},
                })

    def test_image_overlay_dockerfile_is_exclusive(self) -> None:
        for extra in ({"apt": ["jq"]}, {"files": ["f"]}, {"harness_version": "2.1.180"}):
            with self.subTest(extra=extra), self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict("b", {
                    "harness": "claude",
                    "overlay": {"dockerfile": "Dockerfile", **extra},
                })

    def test_image_overlay_dockerfile_must_stay_relative(self) -> None:
        for bad in ("/etc/Dockerfile", "../Dockerfile", "a/../../Dockerfile"):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict("b", {
                    "harness": "claude",
                    "overlay": {"dockerfile": bad},
                })

    def test_overlay_and_custom_dockerfile_rejected_together(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {
                "harness": "claude",
                "dockerfile": "agent.Dockerfile",
                "overlay": {"apt": ["jq"]},
            })

    def test_image_overlay_dockerfile_must_be_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict("b", {
                "harness": "claude",
                "overlay": {"dockerfile": ["Dockerfile"]},
            })

    def test_no_overlay_is_empty(self) -> None:
        p = ManifestAgentProvider.from_dict("b", {"harness": "claude"})
        self.assertTrue(p.overlay.is_empty())

    def test_overlay_unknown_key_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"overlay": {"bogus": 1}})

    def test_overlay_apt_must_be_string_list(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"overlay": {"apt": [1, 2]}})

    def test_overlay_file_escaping_rejected(self) -> None:
        for bad in ("/etc/passwd", "../secret"):
            with self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict(
                    "b", {"overlay": {"files": [bad]}})

    def test_overlay_apt_must_be_debian_package_names(self) -> None:
        # Entries land in a RUN line; a newline would start a new instruction.
        for bad in ("jq\nFROM alpine", "a b", "jq;rm", "-jq", "JQ"):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict("b", {"overlay": {"apt": [bad]}})
        ok = ManifestAgentProvider.from_dict(
            "b", {"overlay": {"apt": ["libssl3:amd64", "g++=4:14.2.0-1", "python3.12"]}})
        self.assertEqual(3, len(ok.overlay.apt))

    def test_overlay_paths_reject_control_characters(self) -> None:
        for key, value in (("files", ["a\nb"]), ("dockerfile", "ov/Docker\tfile")):
            with self.subTest(key=key), self.assertRaises(ManifestError):
                ManifestAgentProvider.from_dict("b", {"overlay": {key: value}})

    def test_builds_custom_image_follows_dockerfile(self) -> None:
        self.assertFalse(ManifestAgentProvider.from_dict("b", {}).builds_custom_image)
        self.assertTrue(ManifestAgentProvider.from_dict(
            "b", {"dockerfile": "agent/Dockerfile"}).builds_custom_image)

    def test_overlay_rejected_for_custom_template(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgentProvider.from_dict(
                "b", {"harness": "my-agent", "overlay": {"apt": ["jq"]}})

    def test_valid_claude_auth_token(self) -> None:
        p = ManifestAgentProvider.from_dict("b", {"harness": "claude", "auth_token": "T"})
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
    def test_a_bottle_is_refused_saying_to_pick_it_at_launch(self) -> None:
        # Agents don't name a bottle (ADR 0011).
        with self.assertRaises(ManifestError) as cm:
            ManifestAgent.from_dict("a", {"bottle": "dev"})
        self.assertIn("agents no longer name a bottle (`bottle: dev`)", str(cm.exception))
        self.assertIn("`--bottle dev`; none means `basic`", str(cm.exception))

    def test_skills_not_list(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"skills": "x"})

    def test_skill_item_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"skills": [5]})

    def test_skill_name_rejects_shell_metacharacters(self) -> None:
        # Skill names become host/guest path segments interpolated into
        # provisioning shell commands; anything outside kebab-case is
        # rejected at load so it can never reach a `bottle.exec` string.
        for bad in ("foo; rm -rf /", "../escape", "foo bar", "Foo", "-leading"):
            with self.assertRaises(ManifestError):
                ManifestAgent.from_dict("a", {"skills": [bad]})

    def test_skill_name_accepts_kebab_case(self) -> None:
        agent = ManifestAgent.from_dict(
            "a", {"skills": ["init-entry", "quality-eval", "skill0"]})
        self.assertEqual(
            agent.skills, ("init-entry", "quality-eval", "skill0")
        )

    def test_prompt_not_string(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"prompt": 5})

    def test_git_gate_repos_rejected_at_agent_level(self) -> None:
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"git-gate": {"repos": {}}})

    def test_git_gate_empty_is_allowed(self) -> None:
        agent = ManifestAgent.from_dict("a", {"git-gate": {}})
        self.assertTrue(agent.git_user.is_empty())


# ---------------------------------------------------------------------------
# Eager ManifestIndex lookup methods
# ---------------------------------------------------------------------------


class TestEagerIndexLookups(unittest.TestCase):
    def _idx(self) -> ManifestIndex:
        return _idx({
            "bottles": {"b": {"git-gate": {"user": {"name": "Bot", "email": "b@x"}}}},
            "agents": {"a": {}},
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
        m = self._idx().load_for_agent("a", ("b",))
        summary = m.git_identity_summary()
        assert summary is not None
        self.assertIn("name=Bot", summary)
        self.assertIn("email=b@x", summary)

    def test_git_identity_summary_none_when_empty(self) -> None:
        m = _idx({"bottles": {"b": {}}, "agents": {"a": {}}}).load_for_agent("a", ("b",))
        self.assertIsNone(m.git_identity_summary())


class TestResolveEnv(unittest.TestCase):
    """resolve_env raises MissingEnvVarError when an interpolated entry's
    host var is unset."""

    def _manifest(self, env_dict: dict[str, str]) -> Manifest:
        idx = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"env": env_dict}},
            "agents": {"demo": {}},
        })
        return idx.load_for_agent("demo", ("dev",))

    def test_missing_interpolated_host_var_raises(self):
        manifest = self._manifest({"API_KEY": "${HOST_API_KEY}"})
        with self.assertRaises(MissingEnvVarError) as cm:
            resolve_env(manifest, MappingSecrets({}))
        self.assertIn("HOST_API_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
