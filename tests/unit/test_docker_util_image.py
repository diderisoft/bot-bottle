"""Unit: commit_container helper in
bot_bottle.backend.docker.util (PRD 0023 chunk 4c additions).

Tests mock `subprocess.run` and assert on argv shape + parsing.
The actual docker round-trip is covered by the chunk 4c
integration smoke."""

from __future__ import annotations

import subprocess
import unittest
from datetime import timezone
from unittest.mock import call, patch

from bot_bottle.backend.docker import util as docker_mod


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:  # type: ignore
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:  # type: ignore
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
    )



class TestImageCreatedAt(unittest.TestCase):
    def test_parses_docker_timestamp_with_nanoseconds(self):
        with patch.object(
            docker_mod.subprocess, "run",
            return_value=_ok(stdout="2026-07-06T15:33:47.123456789Z\n"),
        ) as run:
            created = docker_mod.image_created_at("bot-bottle-claude:latest")
        self.assertIsNotNone(created)
        assert created is not None
        self.assertEqual(2026, created.year)
        self.assertEqual(123456, created.microsecond)
        self.assertEqual(timezone.utc, created.tzinfo)
        self.assertEqual(
            ["docker", "image", "inspect", "--format", "{{.Created}}", "bot-bottle-claude:latest"],
            run.call_args.args[0],
        )

    def test_dies_on_inspect_failure(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_fail("No such image"),
        ), patch.object(
            docker_mod, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                docker_mod.image_created_at("missing:tag")
        die.assert_called_once()
        self.assertIn("missing:tag", die.call_args.args[0])

    def test_returns_none_on_invalid_timestamp(self):
        with patch.object(
            docker_mod.subprocess, "run",
            return_value=_ok(stdout="not-a-timestamp\n"),
        ):
            result = docker_mod.image_created_at("some:tag")
        self.assertIsNone(result)

    def test_returns_none_on_empty_stdout(self):
        with patch.object(
            docker_mod.subprocess, "run",
            return_value=_ok(stdout=""),
        ):
            result = docker_mod.image_created_at("some:tag")
        self.assertIsNone(result)

    def test_parse_docker_timestamp_no_tzinfo_defaults_to_utc(self):
        # A bare datetime with no tz offset should be treated as UTC.
        dt = docker_mod._parse_docker_timestamp("2024-05-01T10:00:00.000000")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(timezone.utc, dt.tzinfo)



class TestCommitContainer(unittest.TestCase):
    def test_runs_docker_commit(self):
        with patch.object(
            docker_mod, "run_docker", return_value=_ok(),
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
            docker_mod, "run_docker", return_value=_fail("No such container"),
        ), patch.object(
            docker_mod, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                docker_mod.commit_container("missing-container", "some:tag")
        die.assert_called_once()
        self.assertIn("missing-container", die.call_args.args[0])

    def test_die_message_includes_image_tag(self):
        with patch.object(
            docker_mod, "run_docker", return_value=_fail("boom"),
        ), patch.object(
            docker_mod, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                docker_mod.commit_container("ctr", "my-tag:v1")
        self.assertIn("my-tag:v1", die.call_args.args[0])


class TestBuildImage(unittest.TestCase):
    def test_passes_build_args_without_mutating_the_value(self):
        with patch.object(
            docker_mod.subprocess, "run", return_value=_ok(),
        ) as run, patch.object(docker_mod, "info"):
            docker_mod.build_image(
                "derived:test",
                "/context",
                dockerfile="Dockerfile.derived",
                build_args={"BASE_IMAGE": "sha256:" + "a" * 64},
            )
        self.assertEqual(
            [
                "docker", "build", "-t", "derived:test",
                "-f", "Dockerfile.derived",
                "--build-arg", "BASE_IMAGE=sha256:" + "a" * 64,
                "/context",
            ],
            run.call_args.args[0],
        )


class TestImageId(unittest.TestCase):
    def test_returns_content_addressed_local_id(self):
        expected = "sha256:" + "b" * 64
        with patch.object(
            docker_mod, "run_docker", return_value=_ok(stdout=expected + "\n"),
        ) as run:
            self.assertEqual(expected, docker_mod.image_id("base:mutable"))
        self.assertEqual(
            [
                "docker", "image", "inspect", "--format", "{{.Id}}",
                "base:mutable",
            ],
            run.call_args.args[0],
        )

    def test_rejects_failed_or_non_content_addressed_inspect(self):
        for result in (_fail("missing"), _ok(stdout="base:latest\n")):
            with self.subTest(result=result), patch.object(
                docker_mod, "run_docker", return_value=result,
            ), patch.object(
                docker_mod, "die", side_effect=SystemExit("die"),
            ) as die:
                with self.assertRaises(SystemExit):
                    docker_mod.image_id("base:latest")
                die.assert_called_once()


class TestPinnedLocalImageRef(unittest.TestCase):
    def test_tags_and_verifies_content_derived_reference(self):
        image = "sha256:" + "c" * 64
        expected = "registry.example:5000/team/base:sha256-" + "c" * 64
        with patch.object(
            docker_mod,
            "image_id",
            side_effect=[image, image],
        ) as image_id, patch.object(
            docker_mod,
            "run_docker",
            return_value=_ok(),
        ) as run:
            self.assertEqual(
                expected,
                docker_mod.pinned_local_image_ref(
                    "registry.example:5000/team/base:mutable"
                ),
            )
        self.assertEqual(
            ["docker", "image", "tag", image, expected],
            run.call_args.args[0],
        )
        self.assertEqual(
            [
                call("registry.example:5000/team/base:mutable"),
                call(expected),
            ],
            image_id.call_args_list,
        )

    def test_rejects_invalid_id_or_failed_tag(self):
        cases = [
            ("sha256:short", _ok()),
            ("sha256:" + "d" * 64, _fail("tag failed")),
        ]
        for image, result in cases:
            with self.subTest(image=image), patch.object(
                docker_mod,
                "image_id",
                return_value=image,
            ), patch.object(
                docker_mod,
                "run_docker",
                return_value=result,
            ), patch.object(
                docker_mod,
                "die",
                side_effect=SystemExit("die"),
            ):
                with self.assertRaises(SystemExit):
                    docker_mod.pinned_local_image_ref("base:latest")


if __name__ == "__main__":
    unittest.main()
