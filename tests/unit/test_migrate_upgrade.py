"""`bottles migrate`: the old egress shape's conversion to the current one
(`manifest/upgrade.py`), written into the file keeping what it can, and a
second run finding nothing to do."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from bot_bottle.manifest.migrate import MigrateError, apply, prepare
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.manifest.upgrade import UpgradeError, upgrade_bottle


def bottle(fm: dict[str, Any]) -> dict[str, Any]:
    return upgrade_bottle(fm)[0]


class _Host(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp) / ".bot-bottle"
        self.bottles = self.root / "bottles"
        self.agents = self.root / "agents"
        self.bottles.mkdir(parents=True)
        self.agents.mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)}))
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def migrate(self, text: str, name: str = "dev") -> str:
        path = self.bottles / f"{name}.md"
        path.write_text(text)
        apply(prepare(name, self.dirs), self.dirs, root=self.root)
        with self.assertRaisesRegex(MigrateError, "nothing to migrate"):
            prepare(name, self.dirs)
        load_bottle_chain(name, self.dirs)
        return path.read_text()



class TestEnv(unittest.TestCase):
    def test_a_bare_question_is_left_as_it_is(self) -> None:
        # `?` is now a plain input; nothing rewrites it to `secret:?`.
        fm = {"env": {"A": "?", "B": "?Paste B"}}
        self.assertEqual((fm, []), upgrade_bottle(fm)[:2])


def route(old: dict[str, Any]) -> dict[str, Any]:
    return bottle({"egress": {"routes": [{"host": "h", **old}]}})["egress"]["routes"][0]


class TestRoutes(unittest.TestCase):
    def test_inspect_false_is_passthrough(self) -> None:
        self.assertEqual({"host": "h", "passthrough": True}, route({"inspect": False}))

    def test_auth_lifts_and_keeps_allowing_any_method(self) -> None:
        # Today an authed route with no matches allowed any method.
        self.assertEqual(
            {"host": "h", "methods": ["*"], "auth": {"type": "bearer", "value": "secret:T"}},
            route({"inspect": {"auth": {"scheme": "Bearer", "token_ref": "T"}}}))
        self.assertEqual({"host": "h", "methods": ["*"], "preserve_auth": True},
                         route({"inspect": {"preserve_auth": True}}))

    def test_matches_are_allow_and_an_entry_without_methods_allowed_any(self) -> None:
        self.assertEqual(
            {"host": "h", "allow": [{"paths": ["/v1/", {"exact": "/x"}, {"regex": "^/y"}],
                                     "methods": ["*"]},
                                    {"methods": ["POST"], "headers": [{"name": "a", "value": "b"}]}],
             "git": {"fetch": True}},
            route({"inspect": {"git": {"fetch": True}, "matches": [
                {"paths": [{"value": "/v1/"}, {"type": "exact", "value": "/x"},
                           {"type": "regex", "value": "^/y"}]},
                {"methods": ["POST"], "headers": [{"name": "a", "value": "b"}]}]}}))

    def test_one_methods_entry_is_the_routes_methods_and_get_head_is_the_default(self) -> None:
        self.assertEqual({"host": "h", "methods": ["GET", "POST"]},
                         route({"inspect": {"matches": [{"methods": ["GET", "POST"]}]}}))
        self.assertEqual({"host": "h"},
                         route({"inspect": {"matches": [{"methods": ["GET", "HEAD"]}]}}))

    def test_the_detectors_fold_into_dlp(self) -> None:
        self.assertEqual({"host": "h", "dlp": False},
                         route({"inspect": {"outbound_detectors": False,
                                            "inbound_detectors": False}}))
        self.assertEqual({"host": "h", "dlp": {"outbound": [], "inbound":
                                               ["naive_injection_detection"],
                                               "on_match": "block"}},
                         route({"inspect": {"outbound_detectors": False,
                                            "inbound_detectors": ["naive_injection_detection"],
                                            "outbound_on_match": "block"}}))

    def test_role_goes_and_a_plain_route_stays(self) -> None:
        self.assertEqual({"host": "h"}, route({"role": []}))
        self.assertEqual({"host": "h"}, route({}))
        current = {"host": "h", "methods": ["GET", "HEAD"], "allow": [{"paths": ["/x"]}]}
        self.assertEqual(current, route(current))

    def test_log_numbers_are_names(self) -> None:
        for number, name in ((0, "off"), (1, "blocked"), (2, "full")):
            self.assertEqual({"egress": {"log": name}}, bottle({"egress": {"log": number}}))

    def test_header_and_scheme_together_has_no_new_form(self) -> None:
        with self.assertRaisesRegex(UpgradeError, "Write it by hand"):
            route({"inspect": {"auth": {"header": "X-A", "scheme": "Bearer", "token_ref": "T"}}})


class TestRoutesInTheFile(_Host):
    def test_the_whole_file_migrates_once_and_loads(self) -> None:
        after = self.migrate(
            "---\n# mine\nextends: basic\negress:\n  log: 2\n  routes:\n"
            "    - host: api.example.com\n      inspect:\n"
            "        auth: { scheme: token, token_ref: GH_PAT }\n"
            "    - host: raw.example.com\n      inspect: false\n---\nBody.\n")
        self.assertTrue(after.startswith("---\n# mine\nextends: basic\n"))
        self.assertEqual({"extends": "basic", "egress": {"log": "full", "routes": [
            {"host": "api.example.com", "methods": ["*"],
             "auth": {"type": "token", "value": "secret:GH_PAT"}},
            {"host": "raw.example.com", "passthrough": True}]}},
            parse_frontmatter(after)[0])


if __name__ == "__main__":
    unittest.main()
