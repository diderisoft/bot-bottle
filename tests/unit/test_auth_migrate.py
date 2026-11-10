"""A route's auth in the old format (`scheme`, `header`, `token_ref`) is
refused at load, saying to run `bottles migrate`, which rewrites it as `type`,
`name` and `value` (as the old git-gate format is refused and migrated)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.control.hostctl import bottle_chain, remote_actions
from bot_bottle.manifest import ManifestIndex
from bot_bottle.manifest.egress import OldAuthFormatError, OldRouteFormatError
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from bot_bottle.manifest.migrate import MigrateError, apply, prepare
from bot_bottle.manifest.versions import history
from tests.unit.test_manifest_elements import _Chain


def _refused(bottle: str) -> str:
    return (f"{bottle} egress route api.example.com: inspect is gone (auth, git and "
            f"preserve_auth sit on the route, matches is allow, the detectors are dlp, and "
            f"inspect: false is passthrough: true). Run bot-bottle bottles migrate {bottle}.")


def _bottle(auth: "str | list[str]", *, flow: bool = True) -> str:
    """A bottle with one authed route; `auth` is the flow mapping's body, or
    (not `flow`) the block's lines."""
    head = ("---\n"
            "# the dev bottle\n"
            "extends: basic\n"
            "egress:\n"
            "  routes:\n"
            "    - host: api.example.com   # keep me\n"
            "      inspect:\n")
    tail = ("        matches:\n"
            "          - methods: [GET]\n"
            "    - host: raw.example.com\n"
            "---\n"
            "\n"
            "The dev bottle.\n")
    if flow:
        return head + f"        auth: {{ {auth} }}\n" + tail
    return head + "        auth:\n" + "".join(f"          {line}\n" for line in auth) + tail


class _Host(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp) / ".bot-bottle"
        self.bottles = self.root / "bottles"
        self.bottles.mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)}))
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def write(self, text: str, name: str = "dev") -> Path:
        path = self.bottles / f"{name}.md"
        path.write_text(text)
        return path


class TestRefused(_Host):
    def test_loading_says_to_migrate(self) -> None:
        for text in (_bottle("scheme: Bearer, token_ref: EXAMPLE_API_TOKEN"),
                     _bottle(["header: X-Api-Key", "token_ref: K"], flow=False),
                     _bottle("type: bearer, token_ref: K")):
            self.write(text)
            with self.subTest(text), self.assertRaises(OldRouteFormatError) as cm:
                load_bottle_chain("dev", self.dirs)
            self.assertEqual(_refused("dev"), str(cm.exception))

    def test_from_a_json_manifest_too(self) -> None:
        with self.assertRaises(OldAuthFormatError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"egress": {"routes": [{
                    "host": "api.example.com",
                    "auth": {"scheme": "Bearer", "token_ref": "T"}}]}}},
                "agents": {"demo": {}},
            })


class TestMigrate(_Host):
    def migrated(self, before: str) -> str:
        self.write(before)
        return prepare("dev", self.dirs).after

    def auth_after(self, before: str) -> object:
        after = parse_frontmatter(self.migrated(before))[0]
        return after["egress"]["routes"][0]["auth"]  # type: ignore[index]

    def test_each_conversion_in_flow_and_block_style(self) -> None:
        for flow, block, new in (
            ("scheme: Bearer, token_ref: EXAMPLE_API_TOKEN",
             ["scheme: Bearer", "token_ref: \"EXAMPLE_API_TOKEN\"   # the token"],
             {"type": "bearer", "value": "secret:EXAMPLE_API_TOKEN"}),
            ("scheme: token, token_ref: GH_PAT", ["scheme: token", "token_ref: GH_PAT"],
             {"type": "token", "value": "secret:GH_PAT"}),
            ("header: X-Api-Key, token_ref: API_KEY", ["header: X-Api-Key", "token_ref: API_KEY"],
             {"type": "header", "name": "X-Api-Key", "value": "secret:API_KEY"}),
        ):
            with self.subTest(flow):
                self.assertEqual(new, self.auth_after(_bottle(flow)))
                self.assertEqual(new, self.auth_after(_bottle(block, flow=False)))

    def test_what_isnt_egress_stays_as_written(self) -> None:
        after = self.migrated(_bottle("scheme: Bearer, token_ref: A"))
        self.assertTrue(after.startswith("---\n# the dev bottle\nextends: basic\n"))
        self.assertTrue(after.endswith("---\n\nThe dev bottle.\n"))

    def test_the_result_loads_and_is_a_version(self) -> None:
        path = self.write(_bottle("header: X-Api-Key, token_ref: API_KEY"))
        sha = apply(prepare("dev", self.dirs), self.dirs, root=self.root)
        route = next(r for r in load_bottle_chain("dev", self.dirs)[0].egress.routes
                     if r.Host == "api.example.com")
        self.assertEqual(("", "x-api-key", "API_KEY"),
                         (route.AuthScheme, route.AuthHeader, route.TokenRef))
        versions = history(path, root=self.root)
        self.assertEqual(sha, versions[0].sha)
        self.assertEqual("bottles migrate: dev to the current format", versions[0].message)

    def test_running_it_again_has_nothing_to_do(self) -> None:
        path = self.write(_bottle("scheme: Bearer, token_ref: A"))
        apply(prepare("dev", self.dirs), self.dirs, root=self.root)
        after = path.read_text()
        with self.assertRaisesRegex(MigrateError, "nothing to migrate"):
            prepare("dev", self.dirs)
        self.assertEqual(after, path.read_text())

    def test_header_and_scheme_together_are_left_for_a_hand_edit(self) -> None:
        path = self.write(_bottle("header: X-Auth, scheme: Bearer, token_ref: A"))
        with self.assertRaises(MigrateError) as cm:
            prepare("dev", self.dirs)
        msg = str(cm.exception)
        self.assertIn("X-Auth: Bearer <value>", msg)
        self.assertIn("type: bearer", msg)
        self.assertIn("type: header, name: X-Auth", msg)
        self.assertIn("token_ref: A", path.read_text())

    def test_a_name_that_isnt_a_secret_name_is_left_for_a_hand_edit(self) -> None:
        self.write(_bottle("scheme: Bearer, token_ref: $TOK"))
        with self.assertRaisesRegex(MigrateError, "edit it by hand"):
            prepare("dev", self.dirs)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


OLD_WORK = ("---\nextends: base\negress:\n  routes:\n    - host: api.example.com\n"
            "      inspect:\n        auth: { scheme: Bearer, token_ref: API_TOKEN }\n---\n")
NEW_WORK = ("---\nextends: base\negress:\n  routes:\n    - host: api.example.com\n"
            "      auth: { type: bearer, value: secret:API_TOKEN }\n---\n")


class TestConsoleEdits(_Chain):
    def test_a_bottle_write_in_the_old_format_is_refused_and_put_back(self) -> None:
        path = self.bottles / "work.md"
        before = path.read_text()
        with patch("bot_bottle.manifest.versions.record", return_value="v2"):
            reply = asyncio.run(remote_actions.bottle_write(
                {"agent_id": "a1", "bottle": "work", "digest": _digest(before),
                 "content": OLD_WORK}, {"a1": "s1"}))
        self.assertIn(_refused("work"), reply["error"])
        self.assertEqual(before, path.read_text())

    def test_a_bottle_write_in_the_new_format_is_saved(self) -> None:
        path = self.bottles / "work.md"
        with patch("bot_bottle.manifest.versions.record", return_value="v2"):
            reply = bottle_chain.write("s1", "work", _digest(path.read_text()),
                                       content=NEW_WORK)
        self.assertTrue(reply["changed"])

    def test_a_manifest_save_on_an_old_format_bottle_is_refused(self) -> None:
        (self.bottles / "work.md").write_text(OLD_WORK)
        reply = asyncio.run(remote_actions.manifest_save(
            {"agent_id": "a1", "digest": "any"}, {"a1": "s1"}))
        self.assertIn(_refused("work"), reply["error"])


if __name__ == "__main__":
    unittest.main()
