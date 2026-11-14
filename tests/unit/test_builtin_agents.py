"""Unit: built-in agents (PRD 0131). `claude` and `codex` come with bot-bottle,
like the built-in `basic` bottle, so they upgrade with it; a host or project
agent of the same name shadows one, and an unchanged copy an earlier `setup`
wrote gives way to the built-in."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.manifest import ManifestIndex
from bot_bottle.manifest.loader import BUILTIN_AGENTS_DIR, scan_agent_names
from bot_bottle.manifest.versions import adopt_builtin_agents, history, record
from bot_bottle.starter import STARTER_MARKER, starter_agent, GENERAL_PROMPT

_ROOT = Path(__file__).resolve().parents[2]


def _only_claude(harness: str) -> bool:
    return harness == "claude"


class _FakeProvider:
    """A provider whose readiness is given, counting the checks."""

    def __init__(self, ready: bool) -> None:
        self.ready, self.calls = ready, 0

    def ready_here(self) -> bool:
        self.calls += 1
        return self.ready


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        (self.root / "agents").mkdir(parents=True)
        env = patch.dict(os.environ, {"HOME": str(self.home), "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def index(self) -> ManifestIndex:
        return ManifestIndex.resolve(str(self.home))

    def agent(self, name: str, text: str) -> Path:
        path = self.root / "agents" / f"{name}.md"
        path.write_text(text)
        return path


class Resolving(_Home):
    def test_listed_where_their_harness_is_ready(self) -> None:
        self.assertEqual({"bottle-architect", "claude", "codex"},
                         set(scan_agent_names(BUILTIN_AGENTS_DIR)))
        with patch("bot_bottle.manifest.index.harness_ready", side_effect=_only_claude):
            names = self.index().all_agent_names
        self.assertIn("claude", names)
        self.assertNotIn("codex", names)  # not set up here: not offered

    def test_named_outright_they_resolve_anywhere(self) -> None:
        for name in ("claude", "codex"):
            self.index().require_agent(name)
            manifest = self.index().load_for_agent(name)
            self.assertEqual(name, manifest.agent.agent_provider.harness)
            self.assertTrue(manifest.agent.agent_provider.forward_host_credentials)
            self.assertTrue(manifest.agent.remote_writable)

    def test_a_host_agent_shadows_a_builtin(self) -> None:
        self.agent("claude",
                   "---\nagent_provider:\n  harness: claude\n  login: host\n---\n\nMine.\n")
        self.assertEqual("Mine.", self.index().load_for_agent("claude").agent.prompt.strip())
        with patch("bot_bottle.manifest.index.harness_ready", return_value=True):
            self.assertEqual(1, self.index().all_agent_names.count("claude"))

    def test_they_are_what_setup_wrote_before(self) -> None:
        # Same frontmatter and prompt as the old starters; only the comment differs.
        def strip(text: str) -> str:
            return "\n".join(line for line in text.splitlines() if not line.startswith("#"))

        for name in ("claude", "codex"):
            old = starter_agent(GENERAL_PROMPT, {"harness": name, "login": "host"})
            self.assertEqual(strip(old), strip((BUILTIN_AGENTS_DIR / f"{name}.md").read_text()))

    def test_they_ship_in_the_package(self) -> None:
        data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
        files = data["tool"]["setuptools"]["package-data"]["bot_bottle"]
        for name in scan_agent_names(BUILTIN_AGENTS_DIR):
            self.assertIn(f"manifest/builtin_agents/{name}.md", files)


class HarnessReady(unittest.TestCase):
    def setUp(self) -> None:
        from bot_bottle import agent_provider  # pylint: disable=import-outside-toplevel
        self.ap = agent_provider
        self.ap._READY.clear()  # pylint: disable=protected-access
        self.addCleanup(self.ap._READY.clear)  # pylint: disable=protected-access

    def test_ready_is_the_providers_answer(self) -> None:
        for ready in (True, False):
            self.ap._READY.clear()  # pylint: disable=protected-access
            with patch.object(self.ap, "get_provider", return_value=_FakeProvider(ready)):
                self.assertEqual(ready, self.ap.harness_ready("claude"))

    def test_checked_at_most_every_half_minute(self) -> None:
        p = _FakeProvider(True)
        with patch.object(self.ap, "get_provider", return_value=p), \
                patch.object(self.ap.time, "monotonic", side_effect=[100.0, 129.0, 131.0]):
            self.ap.harness_ready("codex")
            self.ap.harness_ready("codex")
            self.assertEqual(1, p.calls)
            self.ap.harness_ready("codex")
        self.assertEqual(2, p.calls)

    def test_installed_and_logged_in_for_this_process(self) -> None:
        # claude and codex: their CLI on PATH and a login under HOME.
        from bot_bottle.agent_provider import get_provider  # pylint: disable=import-outside-toplevel
        with tempfile.TemporaryDirectory() as tmp:
            home, bin_dir = Path(tmp) / "home", Path(tmp) / "bin"
            bin_dir.mkdir()
            for name in ("claude", "codex"):
                (bin_dir / name).write_text("#!/bin/sh\n")
                (bin_dir / name).chmod(0o755)
            env = {"HOME": str(home), "PATH": str(bin_dir)}
            with patch.dict(os.environ, env), patch(
                    "bot_bottle.contrib.claude.claude_auth._keychain_item_exists",
                    return_value=False):
                os.environ.pop("CODEX_HOME", None)
                self.assertFalse(get_provider("claude").ready_here())
                self.assertFalse(get_provider("codex").ready_here())
                (home / ".claude").mkdir(parents=True)
                (home / ".claude" / ".credentials.json").write_text("{}")
                (home / ".codex").mkdir()
                (home / ".codex" / "auth.json").write_text(
                    '{"auth_mode": "chatgpt", "tokens": {"access_token": "x"}}')
                self.assertTrue(get_provider("claude").ready_here())
                self.assertTrue(get_provider("codex").ready_here())
                self.assertFalse(get_provider("pi").ready_here())  # no built-in agent

    def test_an_unknown_harness_isnt_ready(self) -> None:
        with patch.object(self.ap, "get_provider", side_effect=ValueError("unknown")):
            self.assertFalse(self.ap.harness_ready("nope"))


class NamedOutright(_Home):
    def test_the_cli_takes_a_built_in_whose_harness_isnt_set_up(self) -> None:
        from bot_bottle.cli.__main__ import _is_agent  # pylint: disable=import-outside-toplevel
        with patch("bot_bottle.manifest.index.harness_ready", return_value=False), \
                patch("os.getcwd", return_value=str(self.home)):
            self.assertTrue(_is_agent("codex"))  # the launch says what to run
            self.assertFalse(_is_agent("nope"))


class ProjectAgentTrust(_Home):
    def test_replacing_a_built_in_is_named_without_its_path(self) -> None:
        from bot_bottle.manifest import trust  # pylint: disable=import-outside-toplevel
        builtin = BUILTIN_AGENTS_DIR / "claude.md"
        self.assertEqual(["replace the built-in agent claude"],
                         trust.host_context("claude", home_agent=builtin))
        self.assertEqual(builtin, self.index().host_agent("claude"))
        mine = self.agent("claude", "---\n---\nMine.\n")
        self.assertEqual(mine, self.index().host_agent("claude"))
        self.assertIsNone(self.index().host_agent("nope"))

    def test_the_trust_command_sees_what_a_launch_does(self) -> None:
        # Both hash the same context, so trusting the agent lets it launch.
        from bot_bottle.cli.commands import trust as trust_cmd  # pylint: disable=import-outside-toplevel
        project = self.home / "proj" / ".bot-bottle" / "agents"
        project.mkdir(parents=True)
        (project / "claude.md").write_text(
            "---\nagent_provider:\n  harness: claude\n---\n\nProject.\n")
        index = ManifestIndex.resolve(str(project.parent.parent))
        found = trust_cmd._project_agent(index, "claude")  # pylint: disable=protected-access
        assert found is not None
        self.assertEqual(BUILTIN_AGENTS_DIR / "claude.md", found[2]["home_agent"])


class AtHostControllerStart(_Home):
    def test_an_unchanged_copy_gives_way(self) -> None:
        from bot_bottle.control.hostctl.__main__ import adopt_builtin_starters  # pylint: disable=import-outside-toplevel
        path = self.agent("claude", starter_agent(GENERAL_PROMPT,
                                                  {"harness": "claude", "login": "host"}))
        record("setup: write agents/claude", root=self.root)
        adopt_builtin_starters()
        self.assertFalse(path.exists())

    def test_a_failure_only_warns(self) -> None:
        from bot_bottle.control.hostctl import __main__ as main  # pylint: disable=import-outside-toplevel
        from bot_bottle.manifest.versions import VersionError  # pylint: disable=import-outside-toplevel
        with patch("bot_bottle.manifest.versions.adopt_builtin_agents",
                   side_effect=VersionError("no git")), \
                patch.object(main, "warn") as warn:
            main.adopt_builtin_starters()
        warn.assert_called_once()


class Adopting(_Home):
    def written_by_setup(self, name: str) -> Path:
        path = self.agent(name, starter_agent(GENERAL_PROMPT, {"harness": name, "login": "host"}))
        record(f"setup: write agents/{name}", root=self.root)
        return path

    def test_an_unchanged_setup_copy_gives_way(self) -> None:
        path = self.written_by_setup("claude")
        self.assertIn(STARTER_MARKER, path.read_text())
        self.assertEqual(["claude"], adopt_builtin_agents(["claude", "codex"], root=self.root))
        self.assertFalse(path.exists())
        self.assertIn("Built into bot-bottle", (BUILTIN_AGENTS_DIR / "claude.md").read_text())
        self.assertEqual("built in now: agents/claude", history(path, root=self.root)[0].message)

    def test_a_changed_copy_stays(self) -> None:
        path = self.written_by_setup("codex")
        path.write_text(path.read_text() + "\nAlso: be brief.\n")
        self.assertEqual([], adopt_builtin_agents(["codex"], root=self.root))
        self.assertTrue(path.exists())
        # The hand edit is versioned as such first.
        self.assertEqual("edited by hand", history(path, root=self.root)[0].message)

    def test_one_setup_never_wrote_stays(self) -> None:
        path = self.agent("claude", starter_agent(GENERAL_PROMPT,
                                                  {"harness": "claude", "login": "host"}))
        record("edited by hand", root=self.root)
        self.assertEqual([], adopt_builtin_agents(["claude"], root=self.root))
        self.assertTrue(path.exists())

    def test_an_older_setup_version_gives_way_too(self) -> None:
        path = self.written_by_setup("claude")
        path.write_text("an older starter\n")
        record("setup: write agents/claude", root=self.root)  # `setup --force`
        path.write_text(starter_agent(GENERAL_PROMPT, {"harness": "claude", "login": "host"}))
        self.assertEqual(["claude"], adopt_builtin_agents(["claude"], root=self.root))

    def test_only_setups_own_commit_counts(self) -> None:
        # The same text committed under another name's setup message isn't it.
        path = self.agent("claude", starter_agent(GENERAL_PROMPT,
                                                  {"harness": "claude", "login": "host"}))
        record("setup: write agents/claude-old", root=self.root)
        self.assertEqual([], adopt_builtin_agents(["claude"], root=self.root))
        self.assertTrue(path.exists())

    def test_compared_byte_for_byte(self) -> None:
        path = self.written_by_setup("codex")
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual([], adopt_builtin_agents(["codex"], root=self.root))
        path.write_bytes(b"\xff\xfe not text")
        self.assertEqual([], adopt_builtin_agents(["codex"], root=self.root))
        self.assertEqual(b"\xff\xfe not text", path.read_bytes())

    def test_with_no_copy_nothing_is_committed(self) -> None:
        self.written_by_setup("codex")
        (self.root / "agents" / "helper.md").write_text("hand edit\n")
        self.assertEqual([], adopt_builtin_agents(["claude"], root=self.root))
        log = subprocess.run(["git", "-C", str(self.root), "log", "-1", "--format=%s"],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual("setup: write agents/codex", log)

    def test_without_history_nothing_goes(self) -> None:
        path = self.agent("claude", "anything")
        self.assertEqual([], adopt_builtin_agents(["claude"], root=self.root))
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
