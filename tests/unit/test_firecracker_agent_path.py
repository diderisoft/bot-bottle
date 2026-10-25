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

    def test_pi_is_found_where_its_image_installs_it(self) -> None:
        assignments = bottle._env_assignments_for("node", {})  # pylint: disable=protected-access
        path = next(a for a in assignments if a.startswith("PATH="))
        self.assertIn("/home/node/.pi/agent/node_modules/.bin", path.split("=", 1)[1].split(":"))


if __name__ == "__main__":
    unittest.main()
