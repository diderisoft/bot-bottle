"""Unit: image_id / tag / push helpers in
bot_bottle.backend.docker.util (PRD 0023 chunk 4c additions).

Tests mock `subprocess.run` and assert on argv shape + parsing.
The actual docker round-trip is covered by the chunk 4c
integration smoke."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend.docker import util as docker_mod


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:  # type: ignore
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:  # type: ignore
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
                "sha256:abcdef", docker_mod.image_id("bot-bottle-claude:latest")
            )
        argv = run.call_args.args[0]
        self.assertEqual(
            ["docker", "image", "inspect", "--format", "{{.Id}}", "bot-bottle-claude:latest"],
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


class TestSave(unittest.TestCase):
    def test_save_runs_docker_save(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_ok(),
        ) as run:
            docker_mod.save("bot-bottle-claude:latest", "/tmp/img.tar")
        argv = run.call_args.args[0]
        self.assertEqual(
            ["docker", "save", "bot-bottle-claude:latest", "-o", "/tmp/img.tar"],
            argv,
        )


class TestCommitContainer(unittest.TestCase):
    def test_runs_docker_commit(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_ok(),
        ) as run, patch.object(docker_mod, "info"):
            docker_mod.commit_container(
                "bot-bottle-dev-abc12",
                "bot-bottle-committed-dev-abc12:latest",
            )
        argv = run.call_args.args[0]
        self.assertEqual(
            [
                "docker", "commit",
                "bot-bottle-dev-abc12",
                "bot-bottle-committed-dev-abc12:latest",
            ],
            argv,
        )

    def test_dies_on_docker_commit_failure(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_fail("No such container"),
        ), patch.object(
            docker_mod, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                docker_mod.commit_container("missing-container", "some:tag")
        die.assert_called_once()
        self.assertIn("missing-container", die.call_args.args[0])

    def test_die_message_includes_image_tag(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_fail("boom"),
        ), patch.object(
            docker_mod, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                docker_mod.commit_container("ctr", "my-tag:v1")
        self.assertIn("my-tag:v1", die.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
