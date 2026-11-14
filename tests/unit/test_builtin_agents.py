"""Unit: built-in agents (PRD 0131). `claude` and `codex` come with bot-bottle,
like the built-in `basic` bottle, so they upgrade with it; a host or project
agent of the same name shadows one, and an unchanged copy an earlier `setup`
wrote gives way to the built-in."""

from __future__ import annotations

import os
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
    """A provider whose host detection is given, counting the checks."""

    def __init__(self, installed: bool, logged_in: bool) -> None:
        self.installed, self.logged_in, self.calls = installed, logged_in, 0

    def detect_host(self, _env: object) -> object:
        from bot_bottle.agent_provider import HostDetection  # pylint: disable=import-outside-toplevel
        self.calls += 1
        return HostDetection(installed=self.installed, logged_in=self.logged_in)


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
        self.assertEqual({"claude", "codex"}, set(scan_agent_names(BUILTIN_AGENTS_DIR)))
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

    def provider(self, installed: bool, logged_in: bool) -> "_FakeProvider":
        return _FakeProvider(installed, logged_in)

    def test_ready_means_installed_and_logged_in(self) -> None:
        for installed, logged_in, ready in ((True, True, True), (True, False, False),
                                            (False, False, False)):
            self.ap._READY.clear()  # pylint: disable=protected-access
            with patch.object(self.ap, "get_provider",
                              return_value=self.provider(installed, logged_in)):
                self.assertEqual(ready, self.ap.harness_ready("claude"))

    def test_checked_at_most_every_half_minute(self) -> None:
        p = self.provider(True, True)
        with patch.object(self.ap, "get_provider", return_value=p):
            self.ap.harness_ready("codex")
            self.ap.harness_ready("codex")
        self.assertEqual(1, p.calls)

    def test_an_unknown_harness_isnt_ready(self) -> None:
        with patch.object(self.ap, "get_provider", side_effect=ValueError("unknown")):
            self.assertFalse(self.ap.harness_ready("nope"))


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

    def test_without_history_nothing_goes(self) -> None:
        path = self.agent("claude", "anything")
        self.assertEqual([], adopt_builtin_agents(["claude"], root=self.root))
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
