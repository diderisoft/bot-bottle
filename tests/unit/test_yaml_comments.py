"""Unit: a bottle file's frontmatter comments survive bot-bottle rewriting
it (`contract.yaml_comments`)."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any

from bot_bottle.contract.yaml_comments import keep_comments
from bot_bottle.contract.yaml_subset import parse_frontmatter, serialize_yaml_subset

OLD = """---
# Our team's bottle.
extends: basic
egress:
  routes:
    # npm, for the build
    - host: registry.npmjs.org  # read only
      inspect:
        matches:
          - methods: [GET]
    - host: api.example.com
env:
  NOTE: "a # in quotes"
# the end
---

Body.
"""


def _rewritten(text: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> str:
    fm, body = parse_frontmatter(text)
    fm = change(dict(fm))
    return f"---\n{serialize_yaml_subset(fm)}---\n\n{body.strip()}\n"


class TestKeepComments(unittest.TestCase):
    def test_unchanged_lines_come_back_as_written(self) -> None:
        def add(fm: dict[str, Any]) -> dict[str, Any]:
            fm["egress"]["routes"].append({"host": "api.linear.app"})
            return fm
        out = keep_comments(OLD, _rewritten(OLD, add))
        self.assertIn("# Our team's bottle.\nextends: basic", out)
        self.assertIn("    # npm, for the build\n    - host: registry.npmjs.org  # read only",
                      out)
        self.assertIn("api.linear.app", out)
        self.assertIn("# the end\n---", out)
        env: Any = parse_frontmatter(out)[0]["env"]
        self.assertEqual("a # in quotes", env["NOTE"])
        self.assertEqual(parse_frontmatter(_rewritten(OLD, add))[0], parse_frontmatter(out)[0])
        self.assertTrue(out.endswith("Body.\n"))

    def test_a_comment_on_a_changed_line_is_kept_above_its_replacement(self) -> None:
        def retarget(fm: dict[str, Any]) -> dict[str, Any]:
            fm["egress"]["routes"][0]["host"] = "registry.yarnpkg.com"
            return fm
        out = keep_comments(OLD, _rewritten(OLD, retarget))
        self.assertIn("# npm, for the build", out)
        self.assertIn("# read only", out)
        self.assertLess(out.index("# read only"), out.index("registry.yarnpkg.com"))
        self.assertEqual(parse_frontmatter(_rewritten(OLD, retarget))[0],
                         parse_frontmatter(out)[0])

    def test_a_hash_in_a_block_scalar_is_its_text(self) -> None:
        text = "---\n# c\nrun: |\n  # not a comment\n  echo hi\nb: 1\n---\n"
        self.assertEqual(text, keep_comments(text, text))
        moved = keep_comments(text, "---\nrun: other\nb: 1\n---\n")
        self.assertEqual("---\n# c\nrun: other\nb: 1\n---\n", moved)

    def test_a_value_with_a_hash_is_quoted_so_it_reads_back(self) -> None:
        value: dict[str, object] = {"n": "a # b", "m": "it's # x", "plain": "a#b"}
        written = serialize_yaml_subset(value)
        self.assertIn("plain: a#b", written)
        self.assertEqual(value, parse_frontmatter(f"---\n{written}---\n")[0])

    def test_the_same_value_written_differently_stays_as_written(self) -> None:
        old = '---\n# keys\nkey: "ssh-rsa AAAA"\nlist: [a]\n---\n'
        new = "---\nkey: ssh-rsa AAAA\nlist: [a]\nadded: 1\n---\n\n\n"
        self.assertEqual('---\n# keys\nkey: "ssh-rsa AAAA"\nlist: [a]\nadded: 1\n---\n',
                         keep_comments(old, new))

    def test_without_comments_or_frontmatter_it_is_the_new_text(self) -> None:
        plain = "---\nextends: basic\n---\n\nB.\n"
        self.assertEqual(plain, keep_comments(plain, plain))
        self.assertEqual("x", keep_comments("no frontmatter", "x"))


if __name__ == "__main__":
    unittest.main()
