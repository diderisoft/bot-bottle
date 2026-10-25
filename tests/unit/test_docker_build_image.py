"""Unit: `docker build` finds the Dockerfile wherever bot-bottle runs from.

`docker build -f` resolves against the cwd, not the build context, and the
host controller runs from its own state dir, so a Dockerfile named relative
to the context has to be passed as a path inside it.
"""

from __future__ import annotations

import unittest
from unittest import mock

from bot_bottle.backend.docker import util as docker_util


class BuildImage(unittest.TestCase):
    def build(self, dockerfile: str) -> list[str]:
        with mock.patch.object(docker_util, "run_streaming") as run, \
                mock.patch.object(docker_util.resources, "image_build_args", return_value={}), \
                mock.patch.object(docker_util, "info"):
            docker_util.build_image("t:latest", "/repo", dockerfile=dockerfile)
        return run.call_args.args[0]

    def test_a_relative_dockerfile_is_taken_from_the_context(self) -> None:
        argv = self.build("Dockerfile.orchestrator")
        self.assertEqual("/repo/Dockerfile.orchestrator", argv[argv.index("-f") + 1])
        self.assertEqual("/repo", argv[-1])

    def test_an_absolute_dockerfile_is_left_alone(self) -> None:
        argv = self.build("/elsewhere/Dockerfile")
        self.assertEqual("/elsewhere/Dockerfile", argv[argv.index("-f") + 1])


if __name__ == "__main__":
    unittest.main()
