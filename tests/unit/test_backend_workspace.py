"""Unit: runtime workspace provisioning.

Workspace copy is intentionally handled through
`BottleBackend.provision_workspace` against a running bottle. The
Docker derived-image workspace path stays disabled.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from bot_bottle import bottle_state
from bot_bottle import supervise
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker import DockerBottleBackend
from bot_bottle.backend.smolmachines import SmolmachinesBottleBackend
from bot_bottle.manifest import Manifest


def _manifest() -> Manifest:
    return Manifest.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {
            "demo": {
                "bottle": "dev",
                "skills": [],
                "prompt": "",
            },
        },
    })


def _spec(tmp: Path, *, copy_cwd: bool = True, identity: str = "demo-work") -> BottleSpec:
    return BottleSpec(
        manifest=_manifest(),
        agent_name="demo",
        copy_cwd=copy_cwd,
        user_cwd=str(tmp),
        identity=identity,
    )


def _bottle() -> MagicMock:
    bottle = MagicMock(spec=Bottle)
    bottle.name = "bot-bottle-demo-work"
    bottle.exec.return_value = ExecResult(returncode=0, stdout="", stderr="")
    return bottle


class _FakeStateMixin:
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="backend-workspace.")
        self.tmp = Path(self.tmp_dir.name)
        self.root = self.tmp / ".bot-bottle"
        self.original_root = supervise.bot_bottle_root
        supervise.bot_bottle_root = lambda: self.root  # type: ignore[assignment]

    def tearDown(self) -> None:
        supervise.bot_bottle_root = self.original_root  # type: ignore[assignment]
        self.tmp_dir.cleanup()


class TestRuntimeWorkspaceProvisioning(_FakeStateMixin, unittest.TestCase):
    def test_default_backend_method_copies_workspace_to_running_bottle(self) -> None:
        (self.tmp / "src.txt").write_text("hello\n")
        (self.tmp / ".git").mkdir()
        backend = DockerBottleBackend()

        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(_spec(self.tmp), self.tmp / "stage")

        bottle = _bottle()
        backend.provision_workspace(plan, bottle)

        self.assertEqual(
            [
                call(
                    "rm -rf /home/node/workspace && mkdir -p /home/node",
                    user="root",
                ),
                call(
                    "chown -R node:node /home/node/workspace && "
                    "chmod 755 /home/node/workspace",
                    user="root",
                ),
            ],
            bottle.exec.call_args_list,
        )
        bottle.cp_in.assert_called_once_with(str(self.tmp), "/home/node/workspace")

    def test_default_backend_method_noops_without_copy_cwd(self) -> None:
        backend = DockerBottleBackend()
        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(_spec(self.tmp, copy_cwd=False), self.tmp / "stage")

        bottle = _bottle()
        backend.provision_workspace(plan, bottle)

        bottle.exec.assert_not_called()
        bottle.cp_in.assert_not_called()

    def test_smolmachines_uses_same_running_bottle_method(self) -> None:
        backend = SmolmachinesBottleBackend()
        with patch(
            "bot_bottle.backend.smolmachines.resolve_plan.smolmachines_preflight",
        ):
            plan = backend.prepare(
                _spec(self.tmp, identity="demo-smol-work"),
                self.tmp / "stage",
            )

        bottle = _bottle()
        backend.provision_workspace(plan, bottle)

        bottle.cp_in.assert_called_once_with(str(self.tmp), "/home/node/workspace")
        metadata = bottle_state.read_metadata("demo-smol-work")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("smolmachines", metadata.backend)


class TestWorkspaceTrustPath(_FakeStateMixin, unittest.TestCase):
    def test_prepare_trusts_workspace_path_when_copying_cwd(self) -> None:
        backend = DockerBottleBackend()

        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(_spec(self.tmp), self.tmp / "stage")

        claude_config = self.root / "state" / "demo-work" / "agent" / "claude.json"
        config = claude_config.read_text()
        self.assertIn('"/home/node/workspace"', config)
        self.assertEqual("/home/node/workspace", plan.workspace_plan.workdir)


if __name__ == "__main__":
    unittest.main()
