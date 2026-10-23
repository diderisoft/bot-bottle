"""Unit: project bottles, the built-in `basic`, and trust (PRD 0094)."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli.commands.trust import cmd_trust
from bot_bottle.cli.launch_client import load_trusting
from bot_bottle.lib.log import Die
from bot_bottle.manifest import ManifestIndex, trust

_STRIPE = """---
extends: basic
egress:
  routes:
    - host: api.stripe.com
      inspect:
        matches:
          - methods: [GET, POST]
---
"""


class _Project(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.home, self.project = base / "home", base / "app"
        (self.home / ".bot-bottle" / "agents").mkdir(parents=True)
        (self.home / ".bot-bottle" / "bottles").mkdir(parents=True)
        (self.home / ".bot-bottle" / "agents" / "pi.md").write_text("---\nbottle: pi\n---\n")
        (self.home / ".bot-bottle" / "bottles" / "pi.md").write_text("---\n---\n")
        self.bottles = self.project / ".bot-bottle" / "bottles"
        self.bottles.mkdir(parents=True)
        env = patch.dict(os.environ, {"HOME": str(self.home),
                                      "BOT_BOTTLE_ROOT": str(base / "root")})
        env.start()
        self.addCleanup(env.stop)

    def load(self, *bottles: str):  # type: ignore[no-untyped-def]
        return ManifestIndex.resolve(str(self.project)).load_for_agent("pi", bottles)

    def untrusted(self, *bottles: str) -> trust.UntrustedBottleError:
        with self.assertRaises(trust.UntrustedBottleError) as caught:
            self.load(*bottles)
        return caught.exception


class Basic(_Project):
    def test_basic_is_built_in_and_needs_no_trust(self) -> None:
        manifest = self.load("pi", "basic")
        hosts = {r.Host for r in manifest.bottle.egress.routes}
        self.assertIn("registry.npmjs.org", hosts)
        self.assertIn("github.com", hosts)
        self.assertIn("basic", ManifestIndex.resolve(str(self.project)).all_bottle_names)

    def test_basic_is_read_only_with_git_fetch_on_github(self) -> None:
        routes = {r.Host: r for r in self.load("basic").bottle.egress.routes}
        methods = {m for r in routes.values() for match in r.Matches for m in match.Methods}
        self.assertEqual({"GET", "HEAD"}, methods)
        self.assertTrue(routes["github.com"].GitFetch)
        self.assertFalse(routes["registry.npmjs.org"].GitFetch)

    def test_the_users_own_basic_shadows_the_built_in(self) -> None:
        (self.home / ".bot-bottle" / "bottles" / "basic.md").write_text("---\n---\n")
        self.assertEqual((), self.load("basic").bottle.egress.routes)


class ProjectBottles(_Project):
    def test_an_untrusted_project_bottle_stops_and_shows_only_its_additions(self) -> None:
        (self.bottles / "app.md").write_text(_STRIPE)
        error = self.untrusted("pi", "app")
        self.assertEqual(1, len(error.grants))
        self.assertIn("api.stripe.com", error.grants[0])
        self.assertIn("GET, POST", error.grants[0])
        self.assertIn("untrusted bottle: app", trust.prompt_text(error))

    def test_once_trusted_it_loads_and_a_change_asks_again_with_the_diff(self) -> None:
        (self.bottles / "app.md").write_text(_STRIPE)
        trust.trust(self.untrusted("pi", "app"))
        hosts = {r.Host for r in self.load("pi", "app").bottle.egress.routes}
        self.assertIn("api.stripe.com", hosts)
        (self.bottles / "app.md").write_text(_STRIPE.replace(
            "          - methods: [GET, POST]\n",
            "          - methods: [GET, POST]\n    - host: api.segment.io\n"))
        changed = self.untrusted("pi", "app")
        self.assertIsNotNone(changed.previous)
        text = trust.prompt_text(changed)
        self.assertIn("bottle changed: app", text)
        self.assertIn("+ reach   api.segment.io", text)

    def test_a_bottle_that_adds_nothing_beyond_basic_needs_no_trust(self) -> None:
        (self.bottles / "app.md").write_text("---\nextends: basic\n---\n")
        self.load("pi", "app")

    def test_the_users_bottle_wins_a_name_clash_without_asking(self) -> None:
        (self.bottles / "pi.md").write_text(_STRIPE)
        hosts = {r.Host for r in self.load("pi").bottle.egress.routes}
        self.assertNotIn("api.stripe.com", hosts)

    def test_the_trust_file_is_private(self) -> None:
        (self.bottles / "app.md").write_text(_STRIPE)
        trust.trust(self.untrusted("pi", "app"))
        path = Path(os.environ["BOT_BOTTLE_ROOT"]) / trust.TRUST_FILE
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))


class Launching(_Project):
    def setUp(self) -> None:
        super().setUp()
        (self.bottles / "app.md").write_text(_STRIPE)
        self.index = ManifestIndex.resolve(str(self.project))

    def test_yes_trusts_and_loads(self) -> None:
        with patch("bot_bottle.cli.launch_client.read_tty_line", return_value="y"), \
                patch("sys.stderr", io.StringIO()):
            manifest = load_trusting(self.index, "pi", ("pi", "app"), interactive=True)
        self.assertIn("api.stripe.com", {r.Host for r in manifest.bottle.egress.routes})
        self.assertEqual(1, len(trust.load_trusted()))

    def test_no_stops_the_launch(self) -> None:
        with patch("bot_bottle.cli.launch_client.read_tty_line", return_value=""), \
                patch("sys.stderr", io.StringIO()), self.assertRaises(Die):
            load_trusting(self.index, "pi", ("pi", "app"), interactive=True)
        self.assertEqual([], trust.load_trusted())

    def test_without_a_terminal_it_fails_with_a_pointer(self) -> None:
        with self.assertRaises(trust.UntrustedBottleError) as caught:
            load_trusting(self.index, "pi", ("pi", "app"), interactive=False)
        self.assertIn("bot-bottle trust app", str(caught.exception))


class TrustCommand(_Project):
    def test_trust_yes_then_list(self) -> None:
        (self.bottles / "app.md").write_text(_STRIPE)
        with patch("os.getcwd", return_value=str(self.project)), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, cmd_trust(["app", "--yes"]))
        with patch("builtins.print") as out:
            cmd_trust(["--list"])
        self.assertIn("app", str(out.call_args_list[0]))
        self.load("pi", "app")


if __name__ == "__main__":
    unittest.main()
