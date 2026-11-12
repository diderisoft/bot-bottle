"""Unit: partial bottles (ADR 0021). A bottle whose grants use a forge nothing
in its chain declares loads and can be extended, but can't be launched;
`bottles migrate` adds the declaration from the host's connection."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.backend.base import refuse_partial
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.manifest.extends import merge_bottles_runtime, resolve_bottles
from bot_bottle.manifest.migrate import MigrateError, prepare
from tests.unit._forges import GITEA, GITHUB, connected

GRANT = "gitea.example.com/acme/app:write"
DECLARED: Any = {"gitea.example.com": {"kind": "gitea"}}


@connected(GITEA, GITHUB)
class Marks(unittest.TestCase):
    def test_a_grant_without_a_declaration_is_partial(self) -> None:
        bottle = resolve_bottles({"dev": {"git_gate": {"repos": [GRANT]}}})["dev"]
        self.assertEqual(frozenset({"gitea.example.com"}), bottle.undeclared_forges)
        self.assertEqual(1, len(bottle.git))  # still loads, grant and all

    def test_its_own_declaration_or_a_parents_or_a_childs_completes_it(self) -> None:
        found = resolve_bottles({
            "own": {"git_gate": {"forges": DECLARED, "repos": [GRANT]}},
            "forge": {"git_gate": {"forges": DECLARED}},
            "child": {"extends": "forge", "git_gate": {"repos": [GRANT]}},
            "bare": {"git_gate": {"repos": [GRANT]}},
            "fixed": {"extends": "bare", "git_gate": {"forges": DECLARED}},
        })
        for name in ("own", "child", "fixed"):
            self.assertEqual(frozenset(), found[name].undeclared_forges, name)
        self.assertEqual(frozenset({"gitea.example.com"}), found["bare"].undeclared_forges)

    def test_bottles_launched_together_complete_each_other(self) -> None:
        found = resolve_bottles({"bare": {"git_gate": {"repos": [GRANT]}},
                                 "forge": {"git_gate": {"forges": DECLARED}},
                                 "other": {"git_gate": {"repos": ["github.com/octo/x"]}}})
        self.assertEqual(frozenset(), merge_bottles_runtime(
            [found["bare"], found["forge"]]).undeclared_forges)
        self.assertEqual(frozenset({"github.com", "gitea.example.com"}), merge_bottles_runtime(
            [found["bare"], found["other"]]).undeclared_forges)

    def test_a_launch_of_a_partial_bottle_is_refused_naming_the_fix(self) -> None:
        bottle = resolve_bottles({"dev": {"git_gate": {"repos": [GRANT]}}})["dev"]
        with mock.patch("bot_bottle.backend.base.die", side_effect=SystemExit) as die, \
                self.assertRaises(SystemExit):
            refuse_partial(bottle, ["dev"])
        message = die.call_args.args[0]
        self.assertIn("bottle 'dev' is partial", message)
        self.assertIn("gitea.example.com", message)
        self.assertIn("bot-bottle bottles migrate", message)
        complete = resolve_bottles({"dev": {"git_gate": {"forges": DECLARED,
                                                         "repos": [GRANT]}}})["dev"]
        refuse_partial(complete, ["dev"])  # no error


class Migrate(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.dirs = [(self.dir, False)]

    def _bottle(self, name: str, front: str) -> None:
        (self.dir / f"{name}.md").write_text(f"---\n{front}---\n\nA bottle.\n")

    def test_it_declares_the_forge_from_the_hosts_connection(self) -> None:
        self._bottle("dev", "git_gate:\n  repos:\n    - gitea.example.com/acme/app:write\n")
        with connected(GITEA):
            m = prepare("dev", self.dirs)
        fm = parse_frontmatter(m.after)[0]
        self.assertEqual({"kind": "gitea"},
                         fm["git_gate"]["forges"]["gitea.example.com"])  # type: ignore[index]
        self.assertEqual(["gitea.example.com/acme/app:write"],
                         fm["git_gate"]["repos"])  # type: ignore[index]
        self.assertIn("declares the forge gitea.example.com (from this host's connection)",
                      m.changes)
        self.assertIn("A bottle.", m.after)  # the body stays

    def test_a_forge_a_parent_declares_isnt_declared_again(self) -> None:
        self._bottle("forge", "git_gate:\n  forges:\n    gitea.example.com:\n      kind: gitea\n")
        self._bottle("dev", "extends: forge\ngit_gate:\n  repos:\n"
                            "    - gitea.example.com/acme/app\n")
        with connected(GITEA), self.assertRaisesRegex(MigrateError, "already in the current"):
            prepare("dev", self.dirs)

    def test_a_forge_this_host_hasnt_connected_isnt_invented(self) -> None:
        self._bottle("dev", "git_gate:\n  repos:\n    - github.com/octo/x\n")
        with connected(GITEA), self.assertRaisesRegex(MigrateError, "connect it first"):
            prepare("dev", self.dirs)


class Listing(unittest.TestCase):
    def test_bottles_list_marks_a_partial_bottle(self) -> None:
        # pylint: disable-next=import-outside-toplevel,protected-access
        from bot_bottle.cli.commands.bottles import _row
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "dev.md").write_text(
                "---\ngit_gate:\n  repos:\n    - gitea.example.com/acme/app\n---\n")
            with connected(GITEA), redirect_stdout(io.StringIO()):
                row = _row("dev", [(Path(tmp), False)])
        self.assertEqual("partial: declares no forge for gitea.example.com", row[-1])


if __name__ == "__main__":
    unittest.main()
