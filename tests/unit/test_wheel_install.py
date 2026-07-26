"""Integration: build the wheel, install it into an isolated venv, and prove
the installed distribution is self-contained.

This is the boundary a source-tree existence test can't reach (issue #197
review): under an installed wheel the package lives in ``site-packages`` with
no repo root above it, so anything resolving Dockerfiles / nix / scripts from
``__file__``'s parents would break. Here we install for real and assert that
``bot-bottle doctor`` runs from the console script and that
``bot_bottle.resources`` stages a valid, repo-root-shaped build context.

It does NOT run `start` — building images needs a Docker/KVM host (CI). It
skips cleanly when the build/venv toolchain isn't available.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestWheelInstall(unittest.TestCase):
    """`build` is a declared dev dependency (requirements-dev.txt), so this runs
    in CI. A build/install failure is a real packaging regression and FAILS —
    only genuinely-unsupported infra (no `venv`/`ensurepip`) skips."""

    _tmp: "tempfile.TemporaryDirectory[str]"
    venv_py: Path
    app_root: Path

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
            prefix="bb-wheel-")
        tmp = Path(cls._tmp.name)
        dist = tmp / "dist"

        # A failed wheel build is exactly the regression this test guards — fail,
        # don't skip. `build` is installed via requirements-dev.txt.
        built = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist), str(REPO_ROOT)],
            capture_output=True, text=True, check=False,
        )
        if built.returncode != 0:
            raise AssertionError(f"wheel build failed:\n{built.stderr[-2000:]}")
        wheels = list(dist.glob("*.whl"))
        if not wheels:
            raise AssertionError(f"no wheel produced:\n{built.stdout[-2000:]}")

        # A missing `venv`/`ensurepip` is unsupported optional infra, not a
        # packaging bug — skip only here.
        venv = tmp / "venv"
        made = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                              capture_output=True, text=True, check=False)
        if made.returncode != 0:
            raise unittest.SkipTest(f"venv/ensurepip unavailable:\n{made.stderr[-1500:]}")
        cls.venv_py = venv / "bin" / "python"

        # Installing the freshly-built wheel must succeed — fail if it doesn't.
        install = subprocess.run(
            [
                str(cls.venv_py), "-m", "pip", "install", "--quiet",
                "--force-reinstall", "--no-deps", str(wheels[0]),
            ],
            capture_output=True, text=True, check=False,
        )
        if install.returncode != 0:
            raise AssertionError(f"pip install of the wheel failed:\n{install.stderr[-2000:]}")

        # Isolate the staged build root the wheel writes under the app-data dir.
        cls.app_root = tmp / "appdata"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run(self, tail: "list[str]") -> "subprocess.CompletedProcess[str]":
        """Run the installed venv's python with `tail` appended, from a neutral
        cwd so the source checkout isn't on sys.path — we must import the
        *installed* package, not the repo we built from."""
        env = {"BOT_BOTTLE_ROOT": str(self.app_root), "PATH": "/usr/bin:/bin"}
        return subprocess.run(
            [str(self.venv_py), *tail],
            capture_output=True, text=True, env=env, cwd=self._tmp.name, check=False,
        )

    def test_console_entry_point_installed(self):
        # The `bot-bottle` script the wheel declares must exist in the venv.
        script = self.venv_py.parent / "bot-bottle"
        self.assertTrue(script.is_file(), "bot-bottle console script not installed")

    def test_doctor_runs_from_installed_package(self):
        proc = self._run(["-m", "bot_bottle.cli", "doctor"])
        # doctor exits non-zero here (no backend), but it must RUN and report.
        self.assertIn("python", proc.stdout)
        self.assertIn(proc.returncode, (0, 1))

    def test_installed_wheel_is_self_contained(self):
        # From the installed layout (not a checkout), resources must resolve
        # Dockerfiles and stage a repo-root-shaped build context.
        script = (
            "import bot_bottle.resources as r\n"
            "assert not r.is_source_checkout(), 'should not look like a checkout'\n"
            "assert r.dockerfile('Dockerfile.gateway').is_file()\n"
            "assert r.nix_netpool_module().is_file()\n"
            "assert r.netpool_script().is_file()\n"
            "root = r.build_root()\n"
            "assert (root / 'bot_bottle' / '__init__.py').is_file(), 'no package in context'\n"
            "assert (root / 'pyproject.toml').is_file(), 'no pyproject in context'\n"
            "assert (root / 'Dockerfile.gateway').is_file(), 'no Dockerfile in context'\n"
            "print('SELF_CONTAINED_OK')\n"
        )
        proc = self._run(["-c", script])
        self.assertIn("SELF_CONTAINED_OK", proc.stdout, msg=proc.stderr)


if __name__ == "__main__":
    unittest.main()
