"""Unit: install.sh bootstrapper contract.

The installer is a thin, sudo-free, idempotent bootstrapper. These are
static checks on the script text (no network / no real install) so CI can
run them anywhere: it must be executable, fail-fast, never call sudo,
create the config tree, install the package, and verify with `doctor`.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


def code_only(text: str) -> str:
    """Script text with string literals and comments removed.

    Both are places the script *talks about* commands rather than running
    them — remediation advice quite reasonably says "sudo apt install …" —
    so assertions about what the script actually executes must not see them.
    Strings are stripped before comments because a '#' inside a quoted string
    is not a comment, and several literals here span multiple lines.
    """
    without_strings = re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", "", text)
    return "\n".join(
        ln for ln in without_strings.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )


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
        # The installer must never *invoke* sudo. It may print it: the "no
        # usable python" error suggests 'sudo apt install python3.12'.
        self.assertNotIn("sudo", code_only(self.text))

    def test_creates_config_tree(self):
        self.assertIn(".bot-bottle/agents", self.text)
        self.assertIn(".bot-bottle/bottles", self.text)

    def test_installs_via_pipx_with_venv_fallback(self):
        self.assertIn("pipx install", self.text)
        self.assertIn("-m venv", self.text)

    def test_no_pip_user_fallback(self):
        # `pip install --user` is not a fallback, it's a dead end: PEP 668
        # blocks it on Homebrew, python.org and Debian/Ubuntu interpreters,
        # which is every Python a Mac realistically offers. A private venv is
        # exempt from PEP 668 and needs no bootstrap, since venv is stdlib.
        # code_only, because the comment explaining the absence says the words.
        code = code_only(self.text)
        self.assertNotIn("pip install --user", code)
        self.assertNotIn("--break-system-packages", code)

    def test_venv_lives_under_the_config_dir(self):
        # Keeps the whole install footprint inside ~/.bot-bottle (plus the
        # entry-point symlink), which is what makes deleting a throwaway
        # account a complete reset in scripts/macos-install-test.sh.
        self.assertIn(".bot-bottle/venv", self.text)
        self.assertIn("BOT_BOTTLE_VENV", self.text)

    def test_venv_failure_is_actionable(self):
        # Debian/Ubuntu ship venv separately; failing there must say so rather
        # than dumping ensurepip's error.
        self.assertIn("python3-venv", self.text)

    def test_entry_point_is_exposed_outside_the_venv(self):
        # A venv's bin dir is never on PATH, so the console script has to be
        # linked somewhere conventional or `bot-bottle` is unreachable.
        self.assertIn(".local/bin", self.text)
        self.assertIn("ln -sf", self.text)

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

    def test_installs_into_the_venv_with_its_own_pip(self):
        # The venv's pip, not the base interpreter's — the base one may not
        # exist, and using it would install outside the venv.
        self.assertIn("${VENV}/bin/python\" -m pip install", self.text)

    def test_pipx_is_preferred_when_present(self):
        # The venv is a fallback, not a takeover: someone who already manages
        # their Python apps with pipx keeps doing so.
        self.assertIn("command -v pipx", self.text)

    def test_asks_pipx_where_its_bin_dir_is(self):
        # PIPX_BIN_DIR is configurable, so the post-install "is it on PATH?"
        # check must ask rather than assume ~/.local/bin.
        self.assertIn("PIPX_BIN_DIR", self.text)

    def test_searches_beyond_path_for_an_interpreter(self):
        # `python3` on PATH is the *oldest* interpreter on a stock Mac: a fresh
        # account's PATH is /etc/paths, so python3 is the 3.9.6 CLT stub while
        # the usable build sits somewhere only a shell profile puts on PATH.
        # Giving up at that point dead-ends every new macOS user.
        for candidate in ("python3.11", "/opt/homebrew/bin", "Python.framework"):
            self.assertIn(candidate, self.text)

    def test_interpreter_is_overridable(self):
        self.assertIn("BOT_BOTTLE_PYTHON", self.text)

    def test_pipx_is_pinned_to_the_vetted_interpreter(self):
        # Without --python, pipx builds the venv with whichever interpreter
        # pipx itself was installed with, which need not be the one that
        # passed the version check.
        self.assertIn("pipx install --python", self.text)

    def test_version_failure_is_actionable(self):
        # The failure a new macOS user actually hits must say what to do about
        # it, not just state the requirement.
        self.assertIn("brew install python@", self.text)
        self.assertIn("BOT_BOTTLE_PYTHON=/path/to/python3", self.text)

if __name__ == "__main__":
    unittest.main()
