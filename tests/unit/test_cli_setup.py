"""Unit: `bot-bottle setup` — starters for Claude, Codex and pi (PRD 0091)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bot_bottle.cli import openrouter
from bot_bottle.cli.commands.setup import Setup
from bot_bottle.manifest import ManifestIndex
from bot_bottle.secret_source import FileSecrets

_GEMMA = openrouter.FreeModel("google/gemma-4-26b-a4b-it:free", 262144, 32768, True)


class _Host(unittest.TestCase):
    """A fake host: a temp HOME, a PATH of stub CLIs, and a bot-bottle root."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.bin = Path(tmp.name) / "bin"
        self.root = Path(tmp.name) / "root"
        for d in (self.home, self.bin):
            d.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home), "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        keychain = patch("bot_bottle.contrib.claude.claude_auth._keychain_item_exists",
                         return_value=False)
        keychain.start()
        self.addCleanup(keychain.stop)
        self.lines: list[str] = []
        self.answers: list[str] = []
        self.signed_in = 0
        self.models = [_GEMMA]
        self.dead: set[str] = set()
        self.probed: list[tuple[str, str]] = []

    def install(self, name: str) -> None:
        exe = self.bin / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

    def claude_login(self) -> None:
        creds = self.home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text('{"claudeAiOauth": {"accessToken": "x"}}')

    def codex_login(self) -> None:
        auth = self.home / ".codex" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "x"}}))

    def pi_config(self, provider: str, base_url: str, api_key: str, model: str = "m1") -> None:
        agent = self.home / ".pi" / "agent"
        agent.mkdir(parents=True)
        (agent / "settings.json").write_text(json.dumps(
            {"defaultProvider": provider, "defaultModel": model}))
        (agent / "models.json").write_text(json.dumps({"providers": {provider: {
            "baseUrl": base_url, "api": "openai-completions", "apiKey": api_key,
            "models": [{"id": model, "contextWindow": 65536, "maxTokens": 4096}]}}}))

    def fake_sign_in(self, **_: Any) -> str:
        self.signed_in += 1
        return "sk-or-from-oauth"

    def setup(self, *, interactive: bool = False, extra_env: dict[str, str] | None = None,
              **kwargs: Any) -> Setup:
        return Setup(
            home=self.home,
            host_env={"HOME": str(self.home), "PATH": str(self.bin), **(extra_env or {})},
            interactive=interactive, say=self.lines.append,
            ask=lambda _q: self.answers.pop(0) if self.answers else "",
            sign_in=self.fake_sign_in, lookup=lambda: self.models,
            probe=self.fake_probe, **kwargs)

    def fake_probe(self, model_id: str, key: str) -> bool:
        self.probed.append((model_id, key))
        return model_id not in self.dead

    def starter(self, kind: str, name: str) -> Path:
        return self.home / ".bot-bottle" / kind / f"{name}.md"

    def load(self, agent: str):
        return ManifestIndex.resolve(str(self.home)).load_for_agent(agent)

    def secret(self, name: str) -> str | None:
        return FileSecrets(self.root / "secrets").get(name)


class ClaudeAndCodex(_Host):
    def test_logged_in_providers_get_starters_that_load(self) -> None:
        self.install("claude")
        self.install("codex")
        self.claude_login()
        self.codex_login()
        self.setup().run()
        for name in ("claude", "codex"):
            manifest = self.load(name)
            self.assertEqual(name, manifest.agent.agent_provider.harness)
            self.assertTrue(manifest.agent.agent_provider.forward_host_credentials)

    def test_installed_but_not_logged_in_warns_and_writes_nothing(self) -> None:
        self.install("claude")
        self.setup().run()
        self.assertFalse(self.starter("bottles", "claude").exists())
        self.assertTrue(any("claude login" in line for line in self.lines))

    def test_not_installed_is_skipped_silently(self) -> None:
        self.claude_login()
        self.setup().run()
        self.assertFalse(self.starter("bottles", "claude").exists())
        self.assertFalse(any("claude" in line for line in self.lines))

    def test_never_clobbers_without_force_and_restores_deleted_files(self) -> None:
        self.install("claude")
        self.claude_login()
        self.setup().run()
        self.starter("agents", "claude").write_text("mine")
        self.starter("bottles", "claude").unlink()
        self.setup().run()
        self.assertEqual("mine", self.starter("agents", "claude").read_text())
        self.assertTrue(self.starter("bottles", "claude").exists())
        self.setup(force=True).run()
        self.assertNotEqual("mine", self.starter("agents", "claude").read_text())


class PiOnOpenRouter(_Host):
    def test_without_a_terminal_or_key_pi_is_skipped_with_a_hint(self) -> None:
        self.setup().run()
        self.assertFalse(self.starter("bottles", "pi").exists())
        self.assertTrue(any("sign in to OpenRouter" in line for line in self.lines))
        self.assertEqual(0, self.signed_in)

    def test_signs_in_stores_the_key_and_uses_the_picked_free_model(self) -> None:
        self.setup(interactive=True).run()
        self.assertEqual(1, self.signed_in)
        self.assertEqual("sk-or-from-oauth", self.secret("OPENROUTER_API_KEY"))
        self.assertEqual(0o600, stat.S_IMODE((self.root / "secrets").stat().st_mode))
        self.assertFalse(any("sk-or-from-oauth" in line for line in self.lines))
        settings = self.load("pi").agent.agent_provider.settings
        self.assertEqual(("openrouter", "OPENROUTER_API_KEY"),
                         (settings["provider"], settings["api_key_env"]))
        self.assertEqual((131072, 8192), (settings["context_window"], settings["max_tokens"]))
        self.assertTrue(any("50 requests a day" in line for line in self.lines))

    def test_the_free_router_is_the_default(self) -> None:
        self.setup(interactive=True).run()
        self.assertEqual([(openrouter.FREE_ROUTER, "sk-or-from-oauth")], self.probed)
        self.assertEqual([openrouter.FREE_ROUTER],
                         self.load("pi").agent.agent_provider.settings["models"])

    def test_a_model_that_does_not_answer_is_skipped(self) -> None:
        nemotron = openrouter.FreeModel("nvidia/nemotron-3-super-120b-a12b:free")
        self.models = [nemotron, _GEMMA]
        self.dead = {openrouter.FREE_ROUTER, nemotron.id}
        self.setup(interactive=True).run()
        self.assertEqual([openrouter.FREE_ROUTER, nemotron.id, _GEMMA.id],
                         [m for m, _ in self.probed])
        self.assertEqual([_GEMMA.id], self.load("pi").agent.agent_provider.settings["models"])

    def test_when_nothing_answers_the_free_router_is_used(self) -> None:
        self.dead = {_GEMMA.id, openrouter.FREE_ROUTER}
        self.setup(extra_env={"OPENROUTER_API_KEY": "k"}).run()
        self.assertEqual([openrouter.FREE_ROUTER],
                         self.load("pi").agent.agent_provider.settings["models"])

    def test_a_key_already_in_the_environment_skips_sign_in(self) -> None:
        self.setup(extra_env={"OPENROUTER_API_KEY": "k"}).run()
        self.assertEqual(0, self.signed_in)
        self.assertTrue(self.starter("bottles", "pi").exists())

    def test_an_existing_starter_needs_no_key(self) -> None:
        self.setup(interactive=True).run()
        (self.root / "secrets").unlink()
        self.setup(interactive=True).run()
        self.assertEqual(1, self.signed_in)

    def test_refresh_model_rewrites_only_a_starter_setup_wrote(self) -> None:
        self.dead = {openrouter.FREE_ROUTER}
        self.setup(extra_env={"OPENROUTER_API_KEY": "k"}).run()
        self.models = [openrouter.FreeModel("poolside/laguna-s-2.1:free")]
        self.setup(extra_env={"OPENROUTER_API_KEY": "k"}, refresh_model=True).run()
        self.assertIn("laguna", self.starter("agents", "pi").read_text())
        self.starter("agents", "pi").write_text(
            "---\nbottle: pi\nagent_provider: pi\n---\nmine\n")
        self.setup(extra_env={"OPENROUTER_API_KEY": "k"}, refresh_model=True).run()
        self.assertIn("mine", self.starter("agents", "pi").read_text())


class PiFromTheLocalConfig(_Host):
    def test_a_reachable_provider_with_an_env_key_is_used_as_is(self) -> None:
        self.pi_config("groqish", "https://api.example.com/v1", "EXAMPLE_KEY", "big-model")
        self.setup(extra_env={"EXAMPLE_KEY": "v"}).run()
        settings = self.load("pi").agent.agent_provider.settings
        self.assertEqual(("groqish", ["big-model"], "EXAMPLE_KEY", 65536, 4096),
                         (settings["provider"], settings["models"], settings["api_key_env"],
                          settings["context_window"], settings["max_tokens"]))
        self.assertEqual(0, self.signed_in)

    def test_a_local_ollama_falls_back_to_openrouter(self) -> None:
        self.pi_config("ollama", "http://127.0.0.1:11434/v1", "ollama")
        self.setup(interactive=True).run()
        self.assertEqual(1, self.signed_in)
        self.assertEqual("openrouter",
                         self.load("pi").agent.agent_provider.settings["provider"])

    def test_a_literal_key_is_copied_into_the_secrets_file_when_agreed(self) -> None:
        self.pi_config("my-host", "https://llm.example.com/v1", "sk-literal-value")
        self.answers = ["y"]
        self.setup(interactive=True).run()
        self.assertEqual("sk-literal-value", self.secret("PI_MY_HOST_API_KEY"))
        self.assertEqual("PI_MY_HOST_API_KEY",
                         self.load("pi").agent.agent_provider.settings["api_key_env"])
        self.assertFalse(any("sk-literal-value" in line for line in self.lines))

    def test_a_literal_key_is_left_alone_when_declined(self) -> None:
        self.pi_config("my-host", "https://llm.example.com/v1", "sk-literal-value")
        self.answers = ["n", "y"]  # decline the copy, accept OpenRouter
        self.setup(interactive=True).run()
        self.assertIsNone(self.secret("PI_MY_HOST_API_KEY"))
        self.assertEqual("openrouter",
                         self.load("pi").agent.agent_provider.settings["provider"])


class Doctor(_Host):
    def test_warns_when_the_pi_model_left_the_free_list(self) -> None:
        from bot_bottle.cli.commands import doctor  # pylint: disable=import-outside-toplevel

        self.dead = {openrouter.FREE_ROUTER}  # a single free model, which can vanish
        self.setup(extra_env={"OPENROUTER_API_KEY": "k"}).run()
        with patch.object(openrouter, "listed", return_value=False), \
                patch("builtins.print") as out:
            doctor._check_providers()  # pylint: disable=protected-access
        printed = "\n".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("setup --refresh-model", printed)


if __name__ == "__main__":
    unittest.main()
