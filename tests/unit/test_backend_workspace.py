"""Unit: runtime workspace provisioning.

Workspace copy is intentionally handled through
`BottleBackend.provision_workspace` against a running bottle. The
Docker derived-image workspace path stays disabled.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.secret_source import MappingSecrets
from bot_bottle import bottle_state
from tests.unit import use_bottle_root
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker import DockerBottleBackend
from bot_bottle.backend.firecracker import FirecrackerBottleBackend
from bot_bottle.manifest import ManifestIndex


def _manifest() -> ManifestIndex:
    return ManifestIndex.from_json_obj({
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


def _record_packed(bottle: MagicMock) -> list[str]:
    """The names in the tarball `provision_workspace` copies in."""
    packed: list[str] = []

    def cp_in(src: str, _dst: str) -> None:
        with tarfile.open(src) as tar:
            packed.extend(tar.getnames())

    bottle.cp_in.side_effect = cp_in
    return packed


class _FakeStateMixin:
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="backend-workspace.")
        self.tmp = Path(self.tmp_dir.name)
        self.root = self.tmp / ".bot-bottle"
        self._restore = use_bottle_root(self.root)

    def tearDown(self) -> None:
        self._restore()
        self.tmp_dir.cleanup()


class TestRuntimeWorkspaceProvisioning(_FakeStateMixin, unittest.TestCase):
    def test_default_backend_method_copies_workspace_to_running_bottle(self) -> None:
        (self.tmp / "src.txt").write_text("hello\n")
        (self.tmp / "node_modules").mkdir()
        (self.tmp / "node_modules" / "dep.js").write_text("x")
        backend = DockerBottleBackend(Settings())

        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(_spec(self.tmp), self.tmp / "stage", MappingSecrets({}))

        bottle = _bottle()
        packed = _record_packed(bottle)
        backend.provision_workspace(plan, bottle)

        # One tarball, unpacked in place; dependency directories stay behind.
        bottle.cp_in.assert_called_once_with(ANY, "/tmp/bot-bottle-workspace.tar")
        self.assertIn("src.txt", packed)
        self.assertNotIn("node_modules/dep.js", packed)
        [unpack] = bottle.exec.call_args_list
        self.assertEqual(call(
            "rm -rf /home/node/workspace && mkdir -p /home/node/workspace && "
            "tar -xf /tmp/bot-bottle-workspace.tar -C /home/node/workspace && "
            "rm -f /tmp/bot-bottle-workspace.tar && "
            "chown -R node:node /home/node/workspace && chmod 755 /home/node/workspace",
            user="root"), unpack)

    def test_a_git_workspace_is_filtered_by_gitignore_and_records_its_base(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        (self.tmp / ".gitignore").write_text("secret.env\n.bot-bottle/\n")
        (self.tmp / "app.py").write_text("print(1)\n")
        (self.tmp / "secret.env").write_text("KEY=1\n")
        backend = DockerBottleBackend(Settings())
        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(_spec(self.tmp), self.tmp / "stage", MappingSecrets({}))
        bottle = _bottle()
        packed = _record_packed(bottle)
        backend.provision_workspace(plan, bottle)
        self.assertIn("app.py", packed)
        self.assertIn(".git/HEAD", packed)
        self.assertNotIn("secret.env", packed)
        self.assertIn("bot-bottle-base", bottle.exec.call_args_list[-1].args[0])

    def test_default_backend_method_noops_without_copy_cwd(self) -> None:
        backend = DockerBottleBackend(Settings())
        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(
                _spec(self.tmp, copy_cwd=False), self.tmp / "stage", MappingSecrets({}))

        bottle = _bottle()
        backend.provision_workspace(plan, bottle)

        bottle.exec.assert_not_called()
        bottle.cp_in.assert_not_called()

    def test_firecracker_uses_same_running_bottle_method(self) -> None:
        backend = FirecrackerBottleBackend(Settings())
        with patch(
            "bot_bottle.backend.firecracker.resolve_plan.util.require_firecracker",
        ):
            plan = backend.prepare(
                _spec(self.tmp, identity="demo-fc-work"),
                self.tmp / "stage",
                MappingSecrets({}),
            )

        bottle = _bottle()
        backend.provision_workspace(plan, bottle)

        bottle.cp_in.assert_called_once_with(ANY, "/tmp/bot-bottle-workspace.tar")
        metadata = bottle_state.read_metadata("demo-fc-work")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("firecracker", metadata.backend)


class TestWorkspaceTrustPath(_FakeStateMixin, unittest.TestCase):
    def test_prepare_trusts_workspace_path_when_copying_cwd(self) -> None:
        backend = DockerBottleBackend(Settings())

        with (
            patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"),
            patch(
                "bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                return_value=False,
            ),
        ):
            plan = backend.prepare(_spec(self.tmp), self.tmp / "stage", MappingSecrets({}))

        claude_config = self.root / "state" / "demo-work" / "agent" / "claude.json"
        config = claude_config.read_text()
        self.assertIn('"/home/node/workspace"', config)
        self.assertEqual("/home/node/workspace", plan.workspace_plan.workdir)


if __name__ == "__main__":
    unittest.main()
