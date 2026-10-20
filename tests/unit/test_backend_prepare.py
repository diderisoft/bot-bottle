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

from bot_bottle.contract.hostctl import Settings
from bot_bottle import bottle_state
from tests.unit import use_bottle_root
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker import DockerBottleBackend
from bot_bottle.backend.resolve_common import mint_slug
from bot_bottle.backend.firecracker import FirecrackerBottleBackend
from bot_bottle.manifest import ManifestIndex


def _manifest(*, nested_containers: bool = False) -> ManifestIndex:
    return ManifestIndex.from_json_obj({
        "bottles": {
            "dev": {
                "env": {
                    "LITERAL_ENV": "literal-value",
                    "FORWARDED_ENV": "${HOST_SECRET_ENV}",
                },
                "nested_containers": nested_containers,
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


def _spec(
    tmp: Path, *, identity: str, nested_containers: bool = False,
) -> BottleSpec:
    return BottleSpec(
        manifest=_manifest(nested_containers=nested_containers),
        agent_name="demo",
        copy_cwd=False,
        user_cwd=str(tmp),
        identity=identity,
    )


class _FakeStateMixin:
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="backend-prepare.")
        self.root = Path(self.tmp.name) / ".bot-bottle"
        self._restore = use_bottle_root(self.root)

    def tearDown(self) -> None:
        self._restore()
        self.tmp.cleanup()


class TestDockerPrepare(_FakeStateMixin, unittest.TestCase):
    def test_records_backend_and_preserves_env_split(self) -> None:
        backend = DockerBottleBackend(Settings())
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
        assert metadata is not None
        self.assertEqual("docker", metadata.backend)
        self.assertEqual({"FORWARDED_ENV": "secret-value"}, plan.forwarded_env)
        self.assertEqual("literal-value", plan.agent_provision.guest_env["LITERAL_ENV"])
        self.assertNotIn("FORWARDED_ENV", plan.agent_provision.guest_env)


class TestFirecrackerPrepare(_FakeStateMixin, unittest.TestCase):
    def test_records_backend_and_builds_guest_env(self) -> None:
        backend = FirecrackerBottleBackend(Settings())
        spec = _spec(Path(self.tmp.name), identity="demo-fc")

        with (
            patch.dict("os.environ", {"HOST_SECRET_ENV": "secret-value"}),
            patch(
                "bot_bottle.backend.firecracker.resolve_plan.util.require_firecracker",
            ) as preflight,
        ):
            plan = backend.prepare(spec, Path(self.tmp.name) / "stage")

        preflight.assert_called_once_with(backend.settings.infra)
        metadata = bottle_state.read_metadata("demo-fc")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("firecracker", metadata.backend)
        self.assertEqual(
            "literal-value", plan.agent_provision.guest_env["LITERAL_ENV"],
        )
        self.assertEqual({"FORWARDED_ENV": "secret-value"}, plan.forwarded_env)


class TestNestedContainersRejection(_FakeStateMixin, unittest.TestCase):
    """A backend with no guest-local engine must refuse the flag outright.

    Ignoring it would leave the agent without `docker`, and the only ways to
    fake it here (a host daemon socket, a privileged container) are what
    issue #392 rules out.
    """

    def test_docker_backend_refuses_the_flag(self) -> None:
        backend = DockerBottleBackend(Settings())
        spec = _spec(
            Path(self.tmp.name), identity="demo-docker", nested_containers=True,
        )
        with (
            patch(
                "bot_bottle.backend.resolve_common.die", side_effect=RuntimeError,
            ) as die,
            self.assertRaises(RuntimeError),
        ):
            backend.prepare(spec, Path(self.tmp.name) / "stage")
        self.assertIn("nested_containers", die.call_args.args[0])
        self.assertIn("docker", die.call_args.args[0])

    def test_firecracker_backend_refuses_the_flag(self) -> None:
        backend = FirecrackerBottleBackend(Settings())
        spec = _spec(Path(self.tmp.name), identity="demo-fc", nested_containers=True)
        with (
            patch(
                "bot_bottle.backend.resolve_common.die", side_effect=RuntimeError,
            ) as die,
            self.assertRaises(RuntimeError),
        ):
            backend.prepare(spec, Path(self.tmp.name) / "stage")
        self.assertIn("firecracker", die.call_args.args[0])


class TestMintSlug(unittest.TestCase):
    def _spec(self, *, label: str = "", identity: str = "") -> BottleSpec:
        manifest = _manifest()
        return BottleSpec(
            manifest=manifest,
            agent_name="demo",
            copy_cwd=False,
            user_cwd="/tmp",
            label=label,
            identity=identity,
        )

    def test_no_label_uses_agent_name_with_random_suffix(self) -> None:
        slug = mint_slug(self._spec(label=""))
        self.assertTrue(slug.startswith("demo-"), slug)
        # random suffix present — slug is longer than just "demo"
        self.assertGreater(len(slug), len("demo-"))

    def test_label_becomes_exact_slug(self) -> None:
        slug = mint_slug(self._spec(label="my-run"))
        self.assertEqual("my-run", slug)

    def test_label_with_spaces_slugified_no_suffix(self) -> None:
        slug = mint_slug(self._spec(label="My Feature Run"))
        self.assertEqual("my-feature-run", slug)

    def test_identity_takes_precedence_over_label(self) -> None:
        slug = mint_slug(self._spec(label="my-run", identity="fixed-id"))
        self.assertEqual("fixed-id", slug)


if __name__ == "__main__":
    unittest.main()
