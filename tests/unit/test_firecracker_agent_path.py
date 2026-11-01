"""Unit: a firecracker agent finds its harness's command (#580).

The VM gets no image `ENV`, so the PATH it runs commands with has to carry
every directory a built-in harness's Dockerfile adds with `ENV PATH`.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from bot_bottle.backend.firecracker import bottle

_CONTRIB = Path(bottle.__file__).resolve().parents[2] / "contrib"
_ENV_PATH = re.compile(r'^ENV\s+PATH="?([^"\n]+)"?', re.MULTILINE)


def _path_dirs(dockerfile: Path) -> list[str]:
    """Directories an `ENV PATH=...:${PATH}` line adds."""
    dirs = []
    for value in _ENV_PATH.findall(dockerfile.read_text()):
        dirs += [d for d in value.split(":") if d and "$" not in d]
    return dirs


class AgentPath(unittest.TestCase):
    def test_every_harness_path_dir_is_on_the_vms_path(self) -> None:
        on_path = set(bottle._DEFAULT_PATH_FOR["node"].split(":"))  # pylint: disable=protected-access
        dockerfiles = sorted(_CONTRIB.glob("*/Dockerfile"))
        self.assertTrue(dockerfiles)
        for dockerfile in dockerfiles:
            for directory in _path_dirs(dockerfile):
                with self.subTest(harness=dockerfile.parent.name, directory=directory):
                    self.assertIn(directory, on_path)

    def test_the_image_comes_before_the_kept_home(self) -> None:
        # A home is kept across resume while the rootfs is fresh: nothing in
        # it may shadow the image's harness (a dangling self-update exits 127).
        dirs = bottle._DEFAULT_PATH_FOR["node"].split(":")  # pylint: disable=protected-access
        first_home = min(i for i, d in enumerate(dirs) if d.startswith("/home/"))
        self.assertLess(dirs.index("/usr/local/bin"), first_home)
        self.assertLess(dirs.index("/usr/bin"), first_home)

    def test_pi_is_found_where_its_image_installs_it(self) -> None:
        assignments = bottle._env_assignments_for("node", {})  # pylint: disable=protected-access
        path = next(a for a in assignments if a.startswith("PATH="))
        self.assertIn("/home/node/.pi/agent/node_modules/.bin", path.split("=", 1)[1].split(":"))


if __name__ == "__main__":
    unittest.main()


class TestImageEnv(unittest.TestCase):
    """A built image's ENV applies under the bottle's own env (#731)."""

    def env(self, bottle_env: dict[str, str], image_env: dict[str, str] | None) -> dict[str, str]:
        out = bottle._env_assignments_for("node", bottle_env, image_env)  # pylint: disable=protected-access
        return dict(a.split("=", 1) for a in out)

    def test_the_image_path_replaces_the_default(self):
        got = self.env({}, {"PATH": "/opt/x/bin:/usr/bin", "TOOL": "1"})
        self.assertEqual(("/opt/x/bin:/usr/bin", "1"), (got["PATH"], got["TOOL"]))

    def test_the_bottle_env_wins_and_home_user_stay_ours(self):
        got = self.env({"TOOL": "2", "PATH": "/p"},
                       {"TOOL": "1", "PATH": "/opt/x/bin", "HOME": "/root", "USER": "root"})
        self.assertEqual(("2", "/p", "/home/node", "node"),
                         (got["TOOL"], got["PATH"], got["HOME"], got["USER"]))

    def test_no_image_env_keeps_the_default_path(self):
        self.assertEqual(bottle._DEFAULT_PATH_FOR["node"], self.env({}, None)["PATH"])  # pylint: disable=protected-access

    def test_the_bottle_carries_it_into_exec_and_the_agent(self):
        from unittest import mock
        b = bottle.FirecrackerBottle("c", private_key=Path("/k"), guest_ip="10.0.0.2",
                                     image_env={"TOOL": "1"})
        self.assertIn("TOOL=1", b._agent_remote_argv([]))  # pylint: disable=protected-access
        with mock.patch.object(bottle.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            b.exec("true")
        self.assertIn("TOOL=1", run.call_args.args[0])
