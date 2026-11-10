"""Unit: install.sh bootstrapper contract.

The installer is a thin, sudo-free, idempotent bootstrapper. These are
static checks on the script text (no network / no real install) so CI can
run them anywhere: it must be executable, fail-fast, never call sudo,
create the config tree, install the package, and verify with `doctor`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
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


class TestInstallScript(unittest.TestCase):  # pylint: disable=too-many-public-methods
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

    def test_the_next_step_is_doctors_not_a_fixed_line(self):
        # doctor ends with the next step for this host (`next: ...`); the
        # installer no longer prints its own, often wrong, one.
        self.assertNotIn("next: run '${BOT_BOTTLE_BIN} install'", self.text)
        self.assertNotIn("--help' to get started", self.text)

    def test_runs_bootstrap_reading_the_terminal(self):
        """`bootstrap` is install (asking before its one sudo step), setup and
        doctor. Under `curl | sh`, stdin is the script: their prompts read
        /dev/tty instead, and without a terminal they get /dev/null (install
        then prints the sudo step)."""
        self.assertIn('bootstrap </dev/tty', self.text)
        self.assertIn('bootstrap </dev/null', self.text)
        self.assertNotIn('" setup <', self.text)  # bootstrap runs it

    def test_install_spec_is_overridable(self):
        # Tests / local installs point BOT_BOTTLE_INSTALL_SPEC at a checkout.
        self.assertIn("BOT_BOTTLE_INSTALL_SPEC", self.text)

    def test_installs_the_remote_extra_except_on_intel_macs(self):
        # Pairing a phone needs iroh (the `remote` extra); iroh has no
        # Intel-macOS wheel, decided by the interpreter's machine.
        self.assertIn('bot-bottle[remote] @ ${REPO_SPEC}@${INSTALL_REF}', self.text)
        self.assertIn("platform.machine()", self.text)
        self.assertIn("x86_64", self.text)
        self.assertIn("isn't available here", self.text)

    def test_the_default_spec_needs_git(self):
        # The default (with or without the extra) ends in `.git`, so the git
        # check below still applies to it.
        self.assertIn('REPO_SPEC="git+https://', self.text)
        self.assertIn('.git"', self.text)

    def test_requires_git_for_git_specs(self):
        # A git+ / .git spec (the default) shells out to git; the script must
        # gate on it rather than failing opaquely inside pipx/pip.
        self.assertIn("command -v git", self.text)
        self.assertIn("git+*|*.git", self.text)

    def test_installs_into_the_venv_with_its_own_pip(self):
        # The venv's pip, not the base interpreter's — the base one may not
        # exist, and using it would install outside the venv.
        self.assertIn("${VENV}/bin/python\" -m pip install", self.text)

    def test_uv_is_preferred_then_pipx(self):
        # uv, then pipx, then the venv: whichever tool the user already
        # manages Python apps with keeps doing it.
        uv, pipx = self.text.index("command -v uv"), self.text.index("command -v pipx")
        self.assertLess(uv, pipx)
        self.assertIn('uv tool install --python "${PYTHON}" --force', self.text)

    def test_asks_uv_where_its_bin_dir_is(self):
        self.assertIn("uv tool dir --bin", self.text)

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


_TAG = '{{"name":"{0}","message":"bot-bottle","id":"abc","commit":{{"sha":"abc"}}}}'


def _tags(*names: str) -> str:
    return "[" + ",".join(_TAG.format(n) for n in names) + "]"


# A stub curl serving the tags API from files named by page number: an
# unlisted page is empty (`[]`), or an HTTP error when `fail` exists.
_CURL = '''page=$(printf %s "$*" | sed -n 's/.*page=\\([0-9]*\\).*/\\1/p')
if [ -e "{pages}/$page" ]; then cat "{pages}/$page"; exit 0; fi
[ -e "{pages}/fail" ] && exit 22
echo "[]"'''


class TestInstallRef(unittest.TestCase):
    """Runs install.sh with stub `curl`, `uv`, `git` and `bot-bottle` on PATH:
    which spec it installs, and how tag resolution fails."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self._stub("curl", _CURL.format(pages=self.pages))
        self._stub("uv", f'echo "$@" >> "{self.root}/uv.log"\n'
                         f'[ "$1 $2" = "tool dir" ] && echo "{self.bin}"\nexit 0')
        self._stub("git", "exit 0")
        self._stub("bot-bottle", "exit 0")

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def _page(self, n: int, body: str) -> None:
        (self.pages / str(n)).write_text(body)

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        # Stubs first; the host PATH for coreutils (NixOS has no /usr/bin).
        path = f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        full = {"PATH": path, "HOME": str(self.root / "home"),
                "SHELL": "/bin/sh", **env}
        return subprocess.run(["sh", str(INSTALL_SH), *args], env=full, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)

    def _installed(self) -> str:
        log = (self.root / "uv.log").read_text().splitlines()
        return next(line for line in log if line.startswith("tool install")).split("--force ")[1]

    def test_a_python_without_venv_installs_with_our_uv(self) -> None:
        # Debian and Ubuntu ship venv as python3-venv: with no uv or pipx of
        # the user's, the installer's own uv installs onto that Python.
        if shutil.which("uv") or shutil.which("pipx"):
            self.skipTest("a uv or pipx on this host's PATH would be used first")
        (self.bin / "uv").unlink()
        (self.bin / "python3").unlink()
        self._stub("python3", f'case "$*" in *ensurepip*) exit 1 ;; esac\n'
                              f'exec "{sys.executable}" "$@"')
        ours = self.root / "home" / ".bot-bottle" / "uv" / "bin"
        ours.mkdir(parents=True)
        (ours / "uv").write_text(f'#!/bin/sh\necho "$@" >> "{self.root}/uv.log"\n'
                                 f'[ "$1 $2" = "tool dir" ] && echo "{self.bin}"\nexit 0\n')
        (ours / "uv").chmod(0o755)
        self._page(1, _tags("v0.2.0"))
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        install = next(line for line in (self.root / "uv.log").read_text().splitlines()
                       if line.startswith("tool install"))
        self.assertIn(f"--python {self.bin / 'python3'}", install)
        self.assertNotIn("managed venv", proc.stderr)

    def test_installs_the_newest_release_tag_by_semver_across_pages(self) -> None:
        self._page(1, _tags("v0.9.0", "backup/x", "v0.10.0-rc1", "v0.2.0"))
        self._page(2, _tags("v0.10.0", "v0.1.4"))
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("bot-bottle[remote] @ git+https://github.com/diderisoft/"
                         "bot-bottle.git@v0.10.0", self._installed())
        self.assertIn("installing bot-bottle v0.10.0", proc.stderr)

    def test_main_is_explicit_and_says_it_needs_build(self) -> None:
        for args, env in ((("--main",), {}), ((), {"BOT_BOTTLE_INSTALL_REF": "main"})):
            with self.subTest(args=args, env=env):
                (self.root / "uv.log").unlink(missing_ok=True)
                proc = self._run(*args, **env)
                self.assertEqual(0, proc.returncode, proc.stderr)
                self.assertTrue(self._installed().endswith("bot-bottle.git@main"))
                self.assertIn("bot-bottle up --build", proc.stderr)

    def test_an_explicit_spec_needs_no_tag(self) -> None:
        (self.pages / "fail").touch()
        proc = self._run(BOT_BOTTLE_INSTALL_SPEC="/tmp/bot_bottle.whl")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("/tmp/bot_bottle.whl", self._installed())

    def test_no_tag_fails_clearly_without_falling_back_to_main(self) -> None:
        for broken in ("unreachable", "no release tags"):
            with self.subTest(broken=broken):
                if broken == "unreachable":
                    (self.pages / "fail").touch()
                else:
                    (self.pages / "fail").unlink(missing_ok=True)
                    self._page(1, _tags("backup/x"))
                proc = self._run()
                self.assertNotEqual(0, proc.returncode)
                self.assertIn("couldn't find the latest bot-bottle release", proc.stderr)
                self.assertIn("--main", proc.stderr)
                self.assertFalse((self.root / "uv.log").exists())

    def test_the_package_tools_output_goes_to_a_log(self) -> None:
        self._page(1, _tags("v0.2.0"))
        self._stub("uv", f'echo "$@" >> "{self.root}/uv.log"\n'
                         f'[ "$1 $2" = "tool dir" ] && echo "{self.bin}" && exit 0\n'
                         'echo "Collecting noisy-dependency"; echo "Downloading wheel" >&2\nexit 0')
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("Collecting noisy-dependency", proc.stdout + proc.stderr)
        self.assertIn("installing with uv", proc.stderr)
        logs = list((self.root / "home" / ".bot-bottle" / "logs").glob("install-*.log"))
        self.assertEqual(1, len(logs))
        self.assertIn("Collecting noisy-dependency", logs[0].read_text())
        self.assertIn("Downloading wheel", logs[0].read_text())

    def test_a_failed_install_shows_the_logs_tail_and_where_it_is(self) -> None:
        self._page(1, _tags("v0.2.0"))
        self._stub("uv", f'[ "$1 $2" = "tool dir" ] && echo "{self.bin}" && exit 0\n'
                         'echo "ERROR: no matching distribution for iroh"\nexit 1')
        proc = self._run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("failed; the end of", proc.stderr)
        self.assertIn("    ERROR: no matching distribution for iroh", proc.stderr)
        self.assertIn(".bot-bottle/logs/install-", proc.stderr)

    def test_an_older_bot_bottle_earlier_on_path_isnt_the_one_that_runs(self) -> None:
        """setup and doctor run the entry point just installed, and it says
        another one shadows it on PATH."""
        self._page(1, _tags("v0.2.0"))
        shadow, tools = self.root / "shadow", self.root / "tools"
        shadow.mkdir()
        tools.mkdir()
        for where, who in ((shadow, "old"), (tools, "new")):
            stub = where / "bot-bottle"
            stub.write_text(f'#!/bin/sh\necho "{who} $1" >> "{self.root}/ran.log"\nexit 0\n')
            stub.chmod(0o755)
        self._stub("uv", f'echo "$@" >> "{self.root}/uv.log"\n'
                         f'[ "$1 $2" = "tool dir" ] && echo "{tools}"\nexit 0')
        (self.bin / "bot-bottle").unlink()
        path = f"{shadow}:{self.bin}:{tools}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        proc = self._run(PATH=path)
        self.assertEqual(0, proc.returncode, proc.stderr)
        ran = (self.root / "ran.log").read_text().split()
        self.assertNotIn("old", ran)
        self.assertIn("new", ran)
        self.assertIn(f"'bot-bottle' on your PATH is {shadow}/bot-bottle, not this install",
                      proc.stderr)

    def test_it_says_which_environment_it_sets_up(self) -> None:
        self._page(1, _tags("v0.2.0"))
        proc = self._run(BOT_BOTTLE_ENV="dev")
        self.assertIn("environment dev (BOT_BOTTLE_ENV)", proc.stderr)
        self.assertNotIn("(BOT_BOTTLE_ENV)", self._run().stderr)

    def test_it_ends_with_doctors_next_step(self) -> None:
        self._page(1, _tags("v0.2.0"))
        # bootstrap ends with doctor, whose last line is the next step.
        self._stub("bot-bottle", '[ "$1" = bootstrap ] && echo "next: run \\`bot-bottle\\`"\n'
                                 'exit 0')
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("next: run `bot-bottle`", proc.stdout)
        self.assertIn("installed.", proc.stderr)

    def test_a_bootstrap_that_stops_says_to_run_it_again(self) -> None:
        self._page(1, _tags("v0.2.0"))
        self._stub("bot-bottle", 'exit 1')
        proc = self._run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("bootstrap' again", proc.stderr)

    def test_unknown_arguments_are_refused(self) -> None:
        proc = self._run("--mian")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("unknown argument '--mian'", proc.stderr)


if __name__ == "__main__":
    unittest.main()
