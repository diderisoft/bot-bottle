"""Unit: a bottled agent's home is kept across stop and resume (PRD 0110):
each backend makes it once and says whether it held an earlier run, which
skips the workspace copy and continues the agent's conversation; it goes when
the bottled agent's state does."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle import bottle_state
from bot_bottle.backend.base import home_volume_name
from bot_bottle.backend.cleanup_control import orphan_homes
from bot_bottle.control.hostctl.bottles import agent_args
from tests.unit import use_bottle_root


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(use_bottle_root(self.root))


class TestContinuing(unittest.TestCase):
    def test_onto_a_kept_home_the_agent_continues_its_conversation(self) -> None:
        self.assertEqual("--continue", agent_args("claude", (), "", resume=True)[0])
        self.assertEqual("--continue", agent_args("pi", (), "", resume=True)[0])
        # Codex's is a subcommand, so it comes first.
        self.assertEqual(["resume", "--last"], agent_args("codex", (), "", resume=True)[:2])
        for harness in ("claude", "pi", "codex"):
            with self.subTest(harness=harness):
                fresh = agent_args(harness, (), "")
                self.assertNotIn("--continue", fresh)
                self.assertNotIn("resume", fresh)


class TestKeepingState(_Root):
    def test_any_harness_that_stops_is_kept_for_resume(self) -> None:
        bottle_state.capture_session_state("b1", exit_code=129)  # a console Stop
        self.assertTrue(bottle_state.is_preserved("b1"))
        bottle_state.capture_session_state("b2", exit_code=0)  # it finished
        self.assertFalse(bottle_state.is_preserved("b2"))

    def test_cleanup_removes_only_homes_not_kept_for_resume(self) -> None:
        bottle_state.mark_preserved("kept")
        volumes = [home_volume_name("kept"), home_volume_name("gone"), "bot-bottle-mac-db"]
        self.assertEqual([home_volume_name("gone")], orphan_homes(volumes))

    def test_the_host_controller_removes_a_home_only_once_its_state_goes(self) -> None:
        from bot_bottle.control.hostctl.bottles import BottleService
        backend = mock.Mock()
        service = cast(Any, BottleService.__new__(BottleService))
        bottle_state.mark_preserved("kept")
        service._settle("kept", backend)  # pylint: disable=protected-access
        backend.remove_home.assert_not_called()
        service._settle("gone", backend)  # pylint: disable=protected-access
        backend.remove_home.assert_called_once_with("gone")


class TestWorkspace(unittest.TestCase):
    def test_a_kept_home_keeps_the_agents_workspace(self) -> None:
        # The copy starts with `rm -rf`, which would delete the agent's work.
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        from bot_bottle.contract.hostctl import Settings
        backend = DockerBottleBackend(Settings())
        bottle = mock.Mock(home_kept=True)
        bottle.name = "b"
        plan = SimpleNamespace(workspace_plan=SimpleNamespace(enabled=True, copy_contents=True))
        backend.provision_workspace(cast(Any, plan), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()


def _done(code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", "boom" if code else "")


class TestMacosHome(unittest.TestCase):
    plan = cast(Any, SimpleNamespace(slug="b1", guest_home="/home/node", image="img:1",
                                     container_name="bot-bottle-b1"))

    def test_made_and_seeded_from_the_image_on_its_first_launch(self) -> None:
        from bot_bottle.backend.macos_container import launch
        with mock.patch.object(launch.container_mod, "volume_exists", return_value=False), \
                mock.patch.object(launch.subprocess, "run", return_value=_done()) as run:
            self.assertFalse(launch._ensure_home(self.plan))  # pylint: disable=protected-access
        create, seed = (c.args[0] for c in run.call_args_list)
        self.assertEqual(["container", "volume", "create", "bot-bottle-home-b1"], create)
        self.assertIn("bot-bottle-home-b1:/bb-seed", seed)
        self.assertIn("img:1", seed)
        self.assertIn("cp -a /home/node/. /bb-seed/", seed[-1])

    def test_kept_from_an_earlier_run(self) -> None:
        from bot_bottle.backend.macos_container import launch
        with mock.patch.object(launch.container_mod, "volume_exists", return_value=True), \
                mock.patch.object(launch.subprocess, "run") as run:
            self.assertTrue(launch._ensure_home(self.plan))  # pylint: disable=protected-access
        run.assert_not_called()

    def test_a_failed_seed_leaves_no_half_made_home(self) -> None:
        from bot_bottle.backend.macos_container import launch
        from bot_bottle.lib.log import Die
        with mock.patch.object(launch.container_mod, "volume_exists", return_value=False), \
                mock.patch.object(launch.subprocess, "run", side_effect=[_done(), _done(1)]), \
                mock.patch.object(launch.container_mod, "remove_volume") as remove, \
                self.assertRaises(Die):
            launch._ensure_home(self.plan)  # pylint: disable=protected-access
        remove.assert_called_once_with("bot-bottle-home-b1")


class TestDockerHome(unittest.TestCase):
    plan = cast(Any, SimpleNamespace(slug="b1"))

    def test_made_once_and_kept(self) -> None:
        from bot_bottle.backend.docker import launch
        with mock.patch.object(launch.docker_mod, "volume_exists", return_value=False), \
                mock.patch.object(launch.docker_mod, "create_volume") as create:
            self.assertFalse(launch._ensure_home(self.plan))  # pylint: disable=protected-access
        create.assert_called_once_with("bot-bottle-home-b1")
        with mock.patch.object(launch.docker_mod, "volume_exists", return_value=True), \
                mock.patch.object(launch.docker_mod, "create_volume") as create:
            self.assertTrue(launch._ensure_home(self.plan))  # pylint: disable=protected-access
        create.assert_not_called()


class TestFirecrackerHome(_Root):
    def test_a_disk_in_its_state_dir_made_once(self) -> None:
        from bot_bottle.backend.firecracker import util

        def mke2fs(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            Path(argv[-2]).write_bytes(b"")
            return _done()

        with mock.patch.object(util.subprocess, "run", side_effect=mke2fs) as run:
            path, kept = util.ensure_home_image("b1")
            self.assertEqual((bottle_state.bottle_state_dir("b1") / "home.ext4", False),
                             (path, kept))
            self.assertEqual(util.HOME_IMAGE_SIZE, run.call_args.args[0][-1])
            self.assertEqual((path, True), util.ensure_home_image("b1"))
        self.assertEqual(1, run.call_count)

    def test_the_guest_init_seeds_and_mounts_it(self) -> None:
        from bot_bottle.backend.firecracker.util import _GUEST_INIT
        self.assertIn("cp -a /home/node/. /mnt/bb-home/", _GUEST_INIT)
        self.assertIn("mount /dev/vdb /home/node", _GUEST_INIT)
        # Seeded once: a marker, so a resume keeps what the agent wrote.
        self.assertIn("[ ! -e /mnt/bb-home/.bot-bottle-home ]", _GUEST_INIT)


if __name__ == "__main__":
    unittest.main()
