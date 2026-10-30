"""Unit: a bottle's fingerprint, the same however its file is ordered."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.manifest.fingerprint import bottle_hash
from bot_bottle.manifest.loader import load_bottle_chain

ORDERED = """---
env:
  A: one
  B: two
egress:
  routes:
    - host: example.com
    - host: api.example.com
git-gate:
  repos:
    app:
      url: "ssh://git@h/acme/app.git"
      key: {provider: static, path: /keys/app}
    lib:
      url: "ssh://git@h/acme/lib.git"
      key: {provider: static, path: /keys/lib}
---
"""
# The same bottle, every key, route and repo in another order.
REORDERED = """---
git-gate:
  repos:
    lib:
      url: "ssh://git@h/acme/lib.git"
      key: {provider: static, path: /keys/lib}
    app:
      url: "ssh://git@h/acme/app.git"
      key: {provider: static, path: /keys/app}
egress:
  routes:
    - host: api.example.com
    - host: example.com
env:
  B: two
  A: one
---
"""


class TestBottleHash(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def hash_of(self, text: str, name: str = "b") -> str:
        (self.dir / f"{name}.md").write_text(text)
        return bottle_hash(load_bottle_chain(name, [(self.dir, False)])[0])

    def test_order_in_the_file_doesnt_count(self) -> None:
        self.assertEqual(self.hash_of(ORDERED), self.hash_of(REORDERED))

    def test_a_real_change_does(self) -> None:
        self.assertNotEqual(self.hash_of(ORDERED),
                            self.hash_of(ORDERED.replace("A: one", "A: uno")))
        self.assertNotEqual(self.hash_of(ORDERED),
                            self.hash_of(ORDERED.replace("    - host: example.com\n", "")))

    def test_it_covers_what_a_bottle_extends(self) -> None:
        (self.dir / "base.md").write_text("---\nenv:\n  LANG: C\n---\n")
        child = "---\nextends: base\nenv:\n  A: one\n---\n"
        before = self.hash_of(child, "child")
        (self.dir / "base.md").write_text("---\nenv:\n  LANG: C.UTF-8\n---\n")
        self.assertNotEqual(before, self.hash_of(child, "child"))


if __name__ == "__main__":
    unittest.main()
