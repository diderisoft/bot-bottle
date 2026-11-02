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
from bot_bottle.secret_source import MappingSecrets

_SECRETS = MappingSecrets({"HOST_SECRET_ENV": "secret-value"})


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
        agent_name="demo", bottle_names=("dev",),
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
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker",
            ) as require_docker,
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(spec, Path(self.tmp.name) / "stage", _SECRETS)

        require_docker.assert_called_once_with()
        metadata = bottle_state.read_metadata("demo-docker")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("docker", metadata.backend)
        self.assertEqual({"FORWARDED_ENV": "secret-value"}, plan.forwarded_env)
        self.assertIs(_SECRETS, plan.secrets)
        self.assertEqual("literal-value", plan.agent_provision.guest_env["LITERAL_ENV"])
        self.assertNotIn("FORWARDED_ENV", plan.agent_provision.guest_env)


class TestFirecrackerPrepare(_FakeStateMixin, unittest.TestCase):
    def test_records_backend_and_builds_guest_env(self) -> None:
        backend = FirecrackerBottleBackend(Settings())
        spec = _spec(Path(self.tmp.name), identity="demo-fc")

        with (
            patch(
                "bot_bottle.backend.firecracker.resolve_plan.util.require_firecracker",
            ) as preflight,
        ):
            plan = backend.prepare(spec, Path(self.tmp.name) / "stage", _SECRETS)

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
            backend.prepare(spec, Path(self.tmp.name) / "stage", _SECRETS)
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
            backend.prepare(spec, Path(self.tmp.name) / "stage", _SECRETS)
        self.assertIn("firecracker", die.call_args.args[0])


class TestMintSlug(unittest.TestCase):
    def _spec(self, *, label: str = "", identity: str = "") -> BottleSpec:
        manifest = _manifest()
        return BottleSpec(
            manifest=manifest,
            agent_name="demo", bottle_names=("dev",),
            copy_cwd=False,
            user_cwd="/tmp",
            label=label,
            identity=identity,
        )

    def test_no_identity_mints_a_uuid(self) -> None:
        import uuid
        slug = mint_slug(self._spec(label=""))
        parsed = uuid.UUID(slug)
        self.assertEqual(4, parsed.version)

    def test_label_no_longer_becomes_slug(self) -> None:
        import uuid
        slug = mint_slug(self._spec(label="my-run"))
        # label is now a display field; identity is always a UUID
        uuid.UUID(slug)  # must parse without error

    def test_two_fresh_launches_get_different_identities(self) -> None:
        a = mint_slug(self._spec(label="same-label"))
        b = mint_slug(self._spec(label="same-label"))
        self.assertNotEqual(a, b)

    def test_identity_takes_precedence_over_label(self) -> None:
        slug = mint_slug(self._spec(label="my-run", identity="fixed-id"))
        self.assertEqual("fixed-id", slug)


if __name__ == "__main__":
    unittest.main()
