"""Unit: install.sh bootstrapper contract.

The installer is a thin, sudo-free, idempotent bootstrapper. These are
static checks on the script text (no network / no real install) so CI can
run them anywhere: it must be executable, fail-fast, never call sudo,
create the config tree, install the package, and verify with `doctor`.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


class TestInstallScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL_SH.read_text()

    def test_exists_and_executable(self):
        self.assertTrue(INSTALL_SH.is_file())
        self.assertTrue(os.access(INSTALL_SH, os.X_OK), "install.sh must be executable")

    def test_posix_shebang_and_failfast(self):
        first = self.text.splitlines()[0]
        self.assertEqual("#!/bin/sh", first)
        self.assertIn("set -eu", self.text)

    def test_never_uses_sudo(self):
        # Only executable lines matter; the header comment may mention sudo.
        code = [
            ln for ln in self.text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        self.assertNotIn("sudo", "\n".join(code))

    def test_creates_config_tree(self):
        self.assertIn(".bot-bottle/agents", self.text)
        self.assertIn(".bot-bottle/bottles", self.text)

    def test_installs_via_pipx_with_pip_fallback(self):
        self.assertIn("pipx install", self.text)
        self.assertIn("pip install --user", self.text)

    def test_runs_doctor_after_install(self):
        self.assertIn("doctor", self.text)

    def test_install_spec_is_overridable(self):
        # Tests / local installs point BOT_BOTTLE_INSTALL_SPEC at a checkout.
        self.assertIn("BOT_BOTTLE_INSTALL_SPEC", self.text)

    def test_requires_git_for_git_specs(self):
        # A git+ / .git spec (the default) shells out to git; the script must
        # gate on it rather than failing opaquely inside pipx/pip.
        self.assertIn("command -v git", self.text)
        self.assertIn("git+*|*.git", self.text)

    def test_checks_pip_usable_before_fallback(self):
        self.assertIn("python3 -m pip --version", self.text)

    def test_detects_externally_managed_python(self):
        # PEP 668: 'pip install --user' is blocked on externally-managed
        # interpreters; the script must detect this and point at pipx.
        self.assertIn("EXTERNALLY-MANAGED", self.text)
        self.assertIn("pipx", self.text)


if __name__ == "__main__":
    unittest.main()
