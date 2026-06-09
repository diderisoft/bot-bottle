"""Unit: shared backend prepare wiring.

These tests keep the base `BottleBackend.prepare` template honest:
backend-specific preflight/env hooks must be wired through, and launch
metadata must record the backend that actually prepared the plan.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle import bottle_state
from bot_bottle import supervise
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker import DockerBottleBackend
from bot_bottle.backend.smolmachines import SmolmachinesBottleBackend
from bot_bottle.manifest import Manifest


def _manifest() -> Manifest:
    return Manifest.from_json_obj({
        "bottles": {
            "dev": {
                "env": {
                    "LITERAL_ENV": "literal-value",
                    "FORWARDED_ENV": "${HOST_SECRET_ENV}",
                },
            },
        },
        "agents": {
            "demo": {
                "bottle": "dev",
                "skills": [],
                "prompt": "hello",
            },
        },
    })


def _spec(tmp: Path, *, identity: str) -> BottleSpec:
    return BottleSpec(
        manifest=_manifest(),
        agent_name="demo",
        copy_cwd=False,
        user_cwd=str(tmp),
        identity=identity,
    )


class _FakeStateMixin:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="backend-prepare.")
        self.root = Path(self.tmp.name) / ".bot-bottle"
        self.original_root = supervise.bot_bottle_root
        supervise.bot_bottle_root = lambda: self.root  # type: ignore[assignment]

    def tearDown(self) -> None:
        supervise.bot_bottle_root = self.original_root  # type: ignore[assignment]
        self.tmp.cleanup()


class TestDockerPrepare(_FakeStateMixin, unittest.TestCase):
    def test_records_backend_and_preserves_env_split(self) -> None:
        backend = DockerBottleBackend()
        spec = _spec(Path(self.tmp.name), identity="demo-docker")

        with (
            patch.dict("os.environ", {"HOST_SECRET_ENV": "secret-value"}),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker",
            ) as require_docker,
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(spec, Path(self.tmp.name) / "stage")

        require_docker.assert_called_once_with()
        metadata = bottle_state.read_metadata("demo-docker")
        self.assertIsNotNone(metadata)
        self.assertEqual("docker", metadata.backend)
        self.assertEqual({"FORWARDED_ENV": "secret-value"}, plan.forwarded_env)
        self.assertEqual("literal-value", plan.agent_provision.guest_env["LITERAL_ENV"])
        self.assertNotIn("FORWARDED_ENV", plan.agent_provision.guest_env)


class TestSmolmachinesPrepare(_FakeStateMixin, unittest.TestCase):
    def test_records_backend_and_builds_guest_env(self) -> None:
        backend = SmolmachinesBottleBackend()
        spec = _spec(Path(self.tmp.name), identity="demo-smol")

        with (
            patch.dict("os.environ", {"HOST_SECRET_ENV": "secret-value"}),
            patch(
                "bot_bottle.backend.smolmachines.resolve_plan.smolmachines_preflight",
            ) as preflight,
        ):
            plan = backend.prepare(spec, Path(self.tmp.name) / "stage")

        preflight.assert_called_once_with()
        metadata = bottle_state.read_metadata("demo-smol")
        self.assertIsNotNone(metadata)
        self.assertEqual("smolmachines", metadata.backend)
        self.assertEqual("literal-value", plan.guest_env["LITERAL_ENV"])
        self.assertEqual("secret-value", plan.guest_env["FORWARDED_ENV"])
        self.assertEqual(
            "/etc/ssl/certs/ca-certificates.crt",
            plan.guest_env["SSL_CERT_FILE"],
        )


if __name__ == "__main__":
    unittest.main()
