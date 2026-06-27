"""Unit: lazy (on-disk) ManifestIndex loader branches (coverage ratchet).

The eager from_json_obj path is covered by test_manifest_validation.py;
this drives the lazy resolve()/from_md_dirs path — all_agent_names with a
cwd overlay, load_for_agent on an unknown / malformed agent file, and
require_agent's names-only file-existence checks — so manifest.py's
core-module coverage doesn't depend on the integration suite."""

from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from bot_bottle.manifest import ManifestError, ManifestIndex


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"))


_BOTTLE_DEV = """
    ---
    egress:
      routes:
        - host: example.com
    ---
    The dev bottle.
"""

_AGENT = """
    ---
    bottle: dev
    ---
    An agent.
"""

# Tab in the frontmatter indent -> YamlSubsetError on parse.
_AGENT_BAD_FM = "---\nskills:\n\t- x\n---\nbody\n"


class _LazyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home_root = Path(tempfile.mkdtemp(prefix="cb-home-"))
        self.cwd_root = Path(tempfile.mkdtemp(prefix="cb-cwd-"))
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home_root)

    def tearDown(self) -> None:
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home
        shutil.rmtree(self.home_root, ignore_errors=True)
        shutil.rmtree(self.cwd_root, ignore_errors=True)

    @property
    def home_cb(self) -> Path:
        return self.home_root / ".bot-bottle"

    @property
    def cwd_cb(self) -> Path:
        return self.cwd_root / ".bot-bottle"

    def resolve(self) -> ManifestIndex:
        return ManifestIndex.resolve(str(self.cwd_root))


class TestAllAgentNamesLazy(_LazyCase):
    def test_merges_home_and_cwd_agents(self) -> None:
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "alpha.md", _AGENT)
        _write(self.cwd_cb / "agents" / "beta.md", _AGENT)
        self.assertEqual(["alpha", "beta"], self.resolve().all_agent_names)


class TestLoadForAgentLazy(_LazyCase):
    def test_unknown_agent_raises(self) -> None:
        _write(self.home_cb / "agents" / "alpha.md", _AGENT)
        with self.assertRaises(ManifestError):
            self.resolve().load_for_agent("nope")

    def test_malformed_frontmatter_raises(self) -> None:
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "broken.md", _AGENT_BAD_FM)
        with self.assertRaises(ManifestError):
            self.resolve().load_for_agent("broken")


class TestRequireAgentLazy(_LazyCase):
    def test_existing_home_agent_ok(self) -> None:
        _write(self.home_cb / "agents" / "alpha.md", _AGENT)
        self.resolve().require_agent("alpha")  # no raise

    def test_existing_cwd_agent_ok(self) -> None:
        # File only under cwd -> require_agent's cwd_path branch.
        _write(self.home_cb / "agents" / "alpha.md", _AGENT)
        _write(self.cwd_cb / "agents" / "beta.md", _AGENT)
        self.resolve().require_agent("beta")  # no raise

    def test_unknown_agent_raises(self) -> None:
        _write(self.home_cb / "agents" / "alpha.md", _AGENT)
        with self.assertRaises(ManifestError):
            self.resolve().require_agent("nope")


if __name__ == "__main__":
    unittest.main()
