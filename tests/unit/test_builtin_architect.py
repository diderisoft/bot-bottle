"""Unit: the built-in `bottle-architect` agent (PRD 0131). It ships with
bot-bottle like `claude` and `codex`, runs on `harness: auto` (the first of
claude, codex ready here), and is marked `listed: false`, so it's left out of
lists for picking an agent to launch while it still resolves by name."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.manifest import ManifestError, ManifestIndex, parse_check
from bot_bottle.manifest.account_version import packaged_files
from bot_bottle.manifest.agent import ManifestAgent, agent_from_frontmatter
from bot_bottle.manifest.loader import BUILTIN_AGENTS_DIR, agent_listed
from bot_bottle.manifest.versions import adopt_builtin_agents, record
from bot_bottle.suggest.architect import load_architect

ARCHITECT = "bottle-architect"
BUILTIN = BUILTIN_AGENTS_DIR / f"{ARCHITECT}.md"

# What `setup` wrote before the architect was built in, on a harness.
_OLD_HEADER = """\
# bottle-architect: suggest a bottle for a task (PRD 0117).
# Written by bot-bottle setup.
"""


def _old_setup_copy(harness: str) -> str:
    prompt = BUILTIN.read_text().split("---\n\n", 1)[1]
    return (f"---\n{_OLD_HEADER}agent_provider:\n  harness: {harness}\n  login: host\n"
            f"---\n\n{prompt}")


def _ready(*harnesses: str):
    """`harness_ready` true for exactly `harnesses`, wherever it's asked."""
    def ready(h: str) -> bool:
        return h in harnesses
    return (patch("bot_bottle.agent_provider.harness_ready", side_effect=ready),
            patch("bot_bottle.manifest.index.harness_ready", side_effect=ready))


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
        for p in _ready("claude", "codex"):
            p.start()
            self.addCleanup(p.stop)

    def index(self) -> ManifestIndex:
        return ManifestIndex.resolve(str(self.home))

    def agent(self, name: str, text: str, root: Path | None = None) -> Path:
        path = (root or self.root) / "agents" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class Hidden(_Home):
    def test_left_out_of_lists_but_resolved_by_name(self) -> None:
        index = self.index()
        self.assertNotIn(ARCHITECT, index.all_agent_names)
        self.assertNotIn(ARCHITECT, index.agent_files(listing=True))
        self.assertIn("claude", index.all_agent_names)
        index.require_agent(ARCHITECT)
        self.assertTrue(index.has_agent_named(ARCHITECT))
        self.assertEqual(BUILTIN, index.agent_files()[ARCHITECT])
        agent = index.load_for_agent(ARCHITECT).agent
        self.assertEqual("claude", agent.agent_provider.harness)  # auto, resolved
        self.assertFalse(agent.listed)

    def test_any_agent_can_be_unlisted(self) -> None:
        self.agent("helper", "---\nlisted: false\n---\nHelps.\n")
        self.agent("other", "---\nlisted: true\n---\nOther.\n")
        names = self.index().all_agent_names
        self.assertNotIn("helper", names)
        self.assertIn("other", names)
        self.index().require_agent("helper")

    def test_a_file_that_doesnt_parse_stays_listed(self) -> None:
        # Its launch says what's wrong.
        self.assertTrue(agent_listed(self.agent("broken", "---\nx: [\n---\n")))
        self.assertTrue(agent_listed(self.root / "agents" / "missing.md"))

    def test_eager_mode_too(self) -> None:
        index = ManifestIndex.from_json_obj({"agents": {
            "tool": {"listed": False, "prompt": "x"}, "dev": {"prompt": "y"}}})
        self.assertEqual(["dev"], index.all_agent_names)
        self.assertTrue(index.has_agent_named("tool"))
        self.assertFalse(index.has_agent_named("nope"))

    def test_the_hosts_library_reply_and_launchable_agents_leave_it_out(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import library
        from bot_bottle.control.hostctl.remote_requests import local_agents
        with patch.object(Path, "home", return_value=self.home):
            agents = {a["name"] for a in library.host_library()["agents"]}
            launchable = local_agents()
        self.assertEqual({"claude", "codex"}, agents)
        self.assertNotIn(ARCHITECT, launchable)

    def test_the_cli_names_it_only_as_a_tool(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.commands.agents import cmd_agents
        for tty, shown in ((False, ["claude", "codex"]),
                           (True, ["claude", "codex", f"{ARCHITECT}  (tool)"])):
            out = io.StringIO()
            out.isatty = lambda tty=tty: tty  # type: ignore[method-assign]
            with self.subTest(tty=tty), patch("sys.stdout", out), \
                    patch("os.getcwd", return_value=str(self.home)), \
                    patch("bot_bottle.cli.commands.quick.default_agent", return_value=""):
                self.assertEqual(0, cmd_agents([]))
                self.assertEqual(shown, out.getvalue().splitlines())

    def test_a_dispatch_error_doesnt_offer_it(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            self.index().require_agent("nope")
        self.assertNotIn(ARCHITECT, str(caught.exception))

    def test_start_takes_it_as_an_agent_not_a_task(self) -> None:
        # pylint: disable=import-outside-toplevel
        import argparse
        from bot_bottle.cli.commands.start import _split_task
        args = argparse.Namespace(name=ARCHITECT, task=None, auto=True, prompt=None)
        _split_task(args, self.index())
        self.assertEqual((ARCHITECT, None), (args.name, args.task))


class Replaced(_Home):
    def test_a_library_copy_keeps_what_it_says(self) -> None:
        mine = self.agent(ARCHITECT, "---\nagent_provider:\n  harness: codex\n---\n\nMine.\n")
        index = self.index()
        self.assertIn(ARCHITECT, index.all_agent_names)  # it doesn't say listed: false
        self.assertEqual(mine, index.agent_files()[ARCHITECT])
        self.assertEqual("codex", index.load_for_agent(ARCHITECT).agent.agent_provider.harness)
        got = load_architect(self.root)
        self.assertEqual((mine, "codex", "Mine."), (got.path, got.harness, got.prompt))

    def test_a_library_commits_copy_replaces_it(self) -> None:
        tree = self.home / "commit"
        mine = self.agent(ARCHITECT, "---\nlisted: false\n---\n\nMine.\n", root=tree)
        index = self.index().with_library(tree)
        self.assertEqual(mine, index.agent_files()[ARCHITECT])
        self.assertNotIn(ARCHITECT, index.all_agent_names)

    def test_a_project_copy_replaces_it(self) -> None:
        project = self.home / "proj"
        self.agent(ARCHITECT, "---\n---\n\nProject.\n", root=project / ".bot-bottle")
        index = ManifestIndex.resolve(str(project))
        self.assertEqual(BUILTIN, index.host_agent(ARCHITECT))
        self.assertIn(ARCHITECT, index.all_agent_names)


class Auto(unittest.TestCase):
    def provider(self, block: str):
        fm = f"agent_provider:\n{block}"
        from bot_bottle.contract.yaml_subset import parse_frontmatter  # pylint: disable=import-outside-toplevel
        return agent_from_frontmatter("a", dict(parse_frontmatter(f"---\n{fm}---\nx\n")[0]),
                                      "x").agent_provider

    def test_the_first_ready_of_claude_then_codex(self) -> None:
        agent = ManifestAgent.from_dict("a", {"agent_provider": {"harness": "auto"}})
        for ready, harness in ((("claude", "codex"), "claude"), (("codex", "pi"), "codex")):
            p, _ = _ready(*ready)
            with self.subTest(harness=harness), p:
                self.assertEqual(harness, agent.resolved("a").agent_provider.harness)

    def test_none_ready_is_a_clear_error(self) -> None:
        agent = ManifestAgent.from_dict("a", {"agent_provider": "auto"})
        p, _ = _ready("pi")
        with p, self.assertRaisesRegex(ManifestError, "no harness is ready here.*claude or codex"):
            agent.resolved("a")

    def test_a_named_harness_is_left_alone(self) -> None:
        agent = ManifestAgent.from_dict("a", {"agent_provider": "pi"})
        self.assertIs(agent, agent.resolved("a"))

    def test_it_takes_what_every_auto_harness_takes(self) -> None:
        got = self.provider("  harness: auto\n  login: host\n  settings:\n"
                            "    startup_args: [--verbose]\n  overlay:\n    apt: [jq]\n")
        self.assertEqual(("auto", True, {"startup_args": ["--verbose"]}, ("jq",)),
                         (got.harness, got.forward_host_credentials, got.settings,
                          got.overlay.apt))

    def test_it_refuses_what_one_harness_wouldnt_take(self) -> None:
        cases = (
            ("  harness: auto\n  login:\n    oauth_token: secret:K\n", "must be host"),
            ("  harness: auto\n  login:\n    api_key: secret:K\n", "must be host"),
            ("  harness: auto\n  dockerfile_path: x\n", "dockerfile_path needs a named"),
            ("  harness: auto\n  overlay:\n    harness_version: 1.2.3\n", "harness_version"),
            ("  harness: auto\n  settings:\n    models: [m]\n", "unknown key 'models'"),
        )
        for block, why in cases:
            with self.subTest(why=why), self.assertRaisesRegex(ManifestError, why):
                self.provider(block)

    def test_the_parse_check_takes_it_without_asking_this_host(self) -> None:
        p, _ = _ready()
        with p:
            self.assertEqual("", parse_check.problem(f"agents/{ARCHITECT}.md",
                                                     BUILTIN.read_text()))
        self.assertIn("listed must be true or false",
                      parse_check.problem("agents/x.md", "---\nlisted: \"no\"\n---\nx\n"))
        self.assertIn("must be host", parse_check.problem(
            "agents/x.md", "---\nagent_provider:\n  harness: auto\n  login:\n"
                           "    api_key: secret:K\n---\nx\n"))


class Adopting(_Home):
    def written_by_setup(self, text: str) -> Path:
        path = self.agent(ARCHITECT, text)
        record(f"setup: write agents/{ARCHITECT}", root=self.root)
        return path

    def test_an_unchanged_setup_copy_gives_way(self) -> None:
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                path = self.written_by_setup(_old_setup_copy(harness))
                self.assertEqual([ARCHITECT], adopt_builtin_agents([ARCHITECT], root=self.root))
                self.assertFalse(path.exists())

    def test_a_changed_copy_stays_as_yours(self) -> None:
        path = self.written_by_setup(_old_setup_copy("claude"))
        path.write_text(path.read_text() + "\nAlso: be brief.\n")
        self.assertEqual([], adopt_builtin_agents([ARCHITECT], root=self.root))
        self.assertTrue(path.exists())

    def test_a_copy_on_a_harness_auto_wouldnt_pick_stays(self) -> None:
        # setup chose pi where it was the only harness: this host's choice.
        path = self.written_by_setup(_old_setup_copy("pi"))
        self.assertEqual([], adopt_builtin_agents([ARCHITECT], root=self.root))
        self.assertTrue(path.exists())

    def test_setup_and_the_host_controller_adopt_it(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.commands.setup import Setup
        from bot_bottle.control.hostctl.__main__ import adopt_builtin_starters
        path = self.written_by_setup(_old_setup_copy("codex"))
        adopt_builtin_starters()
        self.assertFalse(path.exists())
        path = self.written_by_setup(_old_setup_copy("claude"))
        lines: list[str] = []
        Setup(home=self.home, host_env={"HOME": str(self.home), "PATH": ""},
              interactive=False, say=lines.append).run()
        self.assertFalse(path.exists())
        self.assertIn(f"  {ARCHITECT} agent: built in now (removed the unchanged copy "
                      "setup wrote)", lines)


class Reported(unittest.TestCase):
    def test_library_builtins_carry_it(self) -> None:
        files = packaged_files()
        self.assertEqual(BUILTIN.read_text(), files[f"agents/{ARCHITECT}.md"])
        self.assertIn("listed: false", files[f"agents/{ARCHITECT}.md"])

    def test_it_ships_in_the_package(self) -> None:
        import tomllib  # pylint: disable=import-outside-toplevel
        data = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
        self.assertIn(f"manifest/builtin_agents/{ARCHITECT}.md",
                      data["tool"]["setuptools"]["package-data"]["bot_bottle"])


if __name__ == "__main__":
    unittest.main()
