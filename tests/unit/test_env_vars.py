"""The `BOT_BOTTLE_*` registry (`bot_bottle/env_vars.py`) stays complete,
holds nothing stale, and the reference docs' tables match it."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from bot_bottle.env_vars import AUDIENCES, BY_NAME, ENV_VARS, NOT_VARIABLES
from scripts import gen_env_docs

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "bot_bottle"
REGISTRY = PKG / "env_vars.py"
# Shell scripts that ship with the package and read BOT_BOTTLE_* vars.
EXTRA_SOURCES = (ROOT / "install.sh", ROOT / "scripts" / "firecracker-netpool.sh")

_NAME = re.compile(r"\bBOT_BOTTLE_[A-Z0-9_]+")


def names_in_sources() -> dict[str, list[str]]:
    """Every BOT_BOTTLE_* name in the package and shipped scripts -> where."""
    found: dict[str, list[str]] = {}
    paths = [p for p in PKG.rglob("*") if p.is_file() and p != REGISTRY
             and "__pycache__" not in p.parts]
    for path in [*sorted(paths), *EXTRA_SOURCES]:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name in set(_NAME.findall(text)):
            found.setdefault(name, []).append(path.relative_to(ROOT).as_posix())
    return found


class TestEnvVarRegistry(unittest.TestCase):
    def test_every_name_in_the_code_is_registered(self) -> None:
        missing = sorted(f"{name} ({', '.join(where)})"
                         for name, where in names_in_sources().items()
                         if name not in BY_NAME and name not in NOT_VARIABLES)
        self.assertEqual([], missing, "add these to bot_bottle/env_vars.py:\n"
                         + "\n".join(missing))

    def test_no_registered_name_is_unused(self) -> None:
        used = names_in_sources()
        stale = sorted(n for n in [*BY_NAME, *NOT_VARIABLES] if n not in used)
        self.assertEqual([], stale, "no longer in the code; delete from "
                         "bot_bottle/env_vars.py:\n" + "\n".join(stale))

    def test_entries_are_well_formed(self) -> None:
        self.assertEqual(len(ENV_VARS), len(BY_NAME), "duplicate names")
        for v in ENV_VARS:
            with self.subTest(name=v.name):
                self.assertRegex(v.name, r"^BOT_BOTTLE_[A-Z0-9_]+[A-Z0-9]$")
                self.assertIn(v.audience, AUDIENCES)
                self.assertTrue(v.description.strip())
                self.assertNotIn("\n", v.description)

    def test_docs_tables_match_the_registry(self) -> None:
        stale = sorted(path.relative_to(ROOT).as_posix()
                       for path, text in gen_env_docs.rendered().items()
                       if path.read_text(encoding="utf-8") != text)
        self.assertEqual([], stale, "run `python3 scripts/gen_env_docs.py`")

    def test_every_audience_is_in_some_table(self) -> None:
        tabled = {a for audiences in gen_env_docs.TABLES.values() for a in audiences}
        self.assertEqual(set(AUDIENCES), tabled)


class TestRender(unittest.TestCase):
    def test_splice_replaces_only_between_markers(self) -> None:
        text = "before\n<!-- env-vars:t:begin -->\nold\n<!-- env-vars:t:end -->\nafter\n"
        self.assertEqual(
            "before\n<!-- env-vars:t:begin -->\nnew\n<!-- env-vars:t:end -->\nafter\n",
            gen_env_docs.splice(text, "t", "new"))

    def test_table_lists_only_its_audiences(self) -> None:
        table = gen_env_docs.render_table(("internal",))
        self.assertIn("`BOT_BOTTLE_ORCHESTRATOR_URL`", table)
        self.assertNotIn("`BOT_BOTTLE_BACKEND`", table)
        self.assertNotIn("| For |", table)


if __name__ == "__main__":
    unittest.main()
