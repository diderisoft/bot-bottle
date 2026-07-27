"""Unit: install.sh bootstrapper contract.

The installer is a thin, sudo-free, idempotent bootstrapper. These are
static checks on the script text (no network / no real install) so CI can
run them anywhere: it must be executable, fail-fast, never call sudo,
create the config tree, install the package, and verify with `doctor`.
"""

from __future__ import annotations

import os
import re
import sysconfig
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
        # Against the discovered interpreter, which is not necessarily the
        # `python3` on PATH — hence no literal command name here.
        self.assertIn("-m pip --version", self.text)

    def test_detects_externally_managed_python(self):
        # PEP 668: 'pip install --user' is blocked on externally-managed
        # interpreters; the script must detect this and point at pipx.
        self.assertIn("EXTERNALLY-MANAGED", self.text)
        self.assertIn("pipx", self.text)

    def test_resolves_user_scripts_dir_not_hardcoded(self):
        # The pip --user scripts dir differs by platform; the script must ask
        # the interpreter (sysconfig + the preferred *user* scheme) rather than
        # hardcoding Linux's ~/.local/bin (which is wrong on macOS python.org).
        self.assertIn("get_preferred_scheme", self.text)
        self.assertIn("sysconfig", self.text)
        # ~/.local/bin appears legitimately elsewhere — it's one of the places
        # the interpreter search looks. What must never happen is USER_SCRIPTS
        # itself being hardcoded to it.
        for line in code_only(self.text).splitlines():
            if "USER_SCRIPTS" in line:
                self.assertNotIn(".local/bin", line)

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

    def test_macos_user_scheme_is_not_dot_local_bin(self):
        # The case the fix exists for: a python.org macOS interpreter uses the
        # osx_framework_user scheme, whose scripts land under
        # ~/Library/Python/<X.Y>/bin — NOT ~/.local/bin. Drive the same
        # sysconfig lookup install.sh uses, with a mac-like userbase, to prove
        # it resolves a non-~/.local/bin directory.
        self.assertIn("osx_framework_user", sysconfig.get_scheme_names())
        scripts = sysconfig.get_path(
            "scripts", "osx_framework_user",
            vars={"userbase": "/Users/dev/Library/Python/3.11"},
        )
        self.assertEqual("/Users/dev/Library/Python/3.11/bin", scripts)
        self.assertNotIn("/.local/bin", scripts)


if __name__ == "__main__":
    unittest.main()
