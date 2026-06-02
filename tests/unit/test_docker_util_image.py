"""Unit: image_id / tag / push helpers in
bot_bottle.backend.docker.util (PRD 0023 chunk 4c additions).

Tests mock `subprocess.run` and assert on argv shape + parsing.
The actual docker round-trip is covered by the chunk 4c
integration smoke."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.docker import util as docker_mod
from bot_bottle.workspace import WorkspacePlan


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


class TestBuildImageWithCwd(unittest.TestCase):
    def test_uses_workspace_plan_paths(self):
        with tempfile.TemporaryDirectory(prefix="bb-docker-cwd.") as tmp:
            workspace = WorkspacePlan(
                enabled=True,
                host_path=Path(tmp),
                guest_home="/guest/home",
                guest_path="/guest/home/workspace",
                workdir="/guest/home/workspace",
            )
            with patch.object(docker_mod.subprocess, "run") as run:
                docker_mod.build_image_with_cwd("derived:tag", "base:tag", workspace)

        argv = run.call_args.args[0]
        dockerfile = run.call_args.kwargs["input"]
        self.assertEqual(["docker", "build", "-t", "derived:tag", "-f", "-"], argv[:6])
        self.assertTrue(argv[6].endswith("/context"))
        self.assertIn("FROM base:tag\n", dockerfile)
        self.assertIn(
            "COPY --chown=node:node workspace/. /guest/home/workspace\n",
            dockerfile,
        )
        self.assertIn("WORKDIR /guest/home/workspace\n", dockerfile)

    def test_staged_context_includes_hidden_files_but_not_git_dir(self):
        with tempfile.TemporaryDirectory(prefix="bb-docker-cwd.") as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("*.pyc\n")
            (root / ".dockerignore").write_text(".gitignore\n")
            (root / ".env.example").write_text("SAFE=1\n")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("[core]\n")
            workspace = WorkspacePlan(
                enabled=True,
                host_path=root,
                guest_home="/guest/home",
                guest_path="/guest/home/workspace",
                workdir="/guest/home/workspace",
            )

            def inspect_context(*args, **kwargs):
                context = Path(args[0][-1])
                staged = context / "workspace"
                self.assertTrue((staged / ".gitignore").is_file())
                self.assertTrue((staged / ".dockerignore").is_file())
                self.assertTrue((staged / ".env.example").is_file())
                self.assertFalse((staged / ".git").exists())
                return _ok()

            with patch.object(
                docker_mod.subprocess, "run", side_effect=inspect_context,
            ):
                docker_mod.build_image_with_cwd("derived:tag", "base:tag", workspace)


if __name__ == "__main__":
    unittest.main()
