"""Unit: image_id / tag / push helpers in
claude_bottle.backend.docker.util (PRD 0023 chunk 4c additions).

Tests mock `subprocess.run` and assert on argv shape + parsing.
The actual docker round-trip is covered by the chunk 4c
integration smoke."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from claude_bottle.backend.docker import util as docker_mod


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
    )


class TestImageId(unittest.TestCase):
    def test_strips_trailing_newline(self):
        # docker image inspect --format ... emits a trailing newline.
        with patch.object(
            docker_mod.subprocess, "run",
            return_value=_ok(stdout="sha256:abcdef\n"),
        ) as run:
            self.assertEqual(
                "sha256:abcdef", docker_mod.image_id("claude-bottle:latest")
            )
        argv = run.call_args.args[0]
        self.assertEqual(
            ["docker", "image", "inspect", "--format", "{{.Id}}", "claude-bottle:latest"],
            argv,
        )

    def test_dies_on_inspect_failure(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_fail("No such image"),
        ), patch.object(
            docker_mod, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                docker_mod.image_id("missing:tag")
            die.assert_called_once()
            self.assertIn("missing:tag", die.call_args.args[0])


class TestTagPush(unittest.TestCase):
    def test_tag_runs_docker_tag(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_ok(),
        ) as run:
            docker_mod.tag("claude-bottle:latest", "localhost:5000/cb:abc")
        argv = run.call_args.args[0]
        self.assertEqual(
            ["docker", "tag", "claude-bottle:latest", "localhost:5000/cb:abc"],
            argv,
        )

    def test_push_runs_docker_push(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_ok(),
        ) as run:
            docker_mod.push("localhost:5000/cb:abc")
        argv = run.call_args.args[0]
        self.assertEqual(["docker", "push", "localhost:5000/cb:abc"], argv)


if __name__ == "__main__":
    unittest.main()
