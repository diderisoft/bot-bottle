"""Firecracker's cp_in, run for real against a local directory: a skill copied
as the directory lands at dst; copied as `src/.` (the old call) it spilled its
files into dst's parent."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.firecracker.bottle import FirecrackerBottle


def _local_ssh(_self: FirecrackerBottle, tty: bool = False) -> list[str]:
    """cp_in runs `[*ssh, "--", remote]`: run `remote` locally instead."""
    del tty
    return ["sh", "-c", 'shift; eval "$1"', "sh"]


def _bottle() -> FirecrackerBottle:
    patch.object(FirecrackerBottle, "_ssh", _local_ssh).start()
    return object.__new__(FirecrackerBottle)


class TestCpInSkill(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.src = self.root / "host" / "verify"
        self.src.mkdir(parents=True)
        (self.src / "SKILL.md").write_text("verify\n")
        self.skills = self.root / "guest" / "skills"
        self.addCleanup(patch.stopall)

    def test_the_directory_lands_at_dst(self) -> None:
        _bottle().cp_in(str(self.src), str(self.skills / "verify"))
        self.assertEqual("verify\n", (self.skills / "verify" / "SKILL.md").read_text())
        self.assertEqual(["verify"], os.listdir(self.skills))

    def test_the_old_contents_form_spilled_into_the_parent(self) -> None:
        (self.skills / "verify").mkdir(parents=True)
        with self.assertRaises(Exception):
            _bottle().cp_in(f"{self.src}/.", f"{self.skills / 'verify'}/")
        self.assertFalse((self.skills / "verify" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
