"""Unit: `preserve_container` keeps a bottled agent's guest (container or VM
disk) across stop and resume, on every backend, so what an agent with root
installs stays (PRD 0110); `resume --fresh` starts a new one, its home kept."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle import bottle_state
from bot_bottle.backend import adoption
from bot_bottle.backend.cleanup_control import kept_guests
from bot_bottle.backend.launch_common import keeps_guest, reuses_guest
from bot_bottle.contract.hostctl import ROLE_CLI, LaunchRequest
from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.extends import merge_bottles_runtime
from bot_bottle.manifest.util import ManifestError
from tests.unit import use_bottle_root


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(use_bottle_root(self.root))


def _plan(preserve: bool = True, fresh: bool = False) -> Any:
    return SimpleNamespace(manifest=SimpleNamespace(bottle=SimpleNamespace(
        preserve_container=preserve)), spec=SimpleNamespace(fresh_guest=fresh))


class TestTheFlag(unittest.TestCase):
    def test_a_bottle_boolean_off_by_default(self) -> None:
        self.assertFalse(ManifestBottle.from_dict("b", {}).preserve_container)
        self.assertTrue(ManifestBottle.from_dict("b", {"preserve_container": True})
                        .preserve_container)
        with self.assertRaises(ManifestError):
            ManifestBottle.from_dict("b", {"preserve_container": "yes"})

    def test_a_later_bottle_can_turn_it_off(self) -> None:
        on = ManifestBottle.from_dict("a", {"preserve_container": True})
        self.assertTrue(merge_bottles_runtime([on, ManifestBottle.from_dict("b", {})])
                        .preserve_container)
        self.assertFalse(merge_bottles_runtime(
            [on, ManifestBottle.from_dict("b", {"preserve_container": False})])
            .preserve_container)

    def test_the_document_has_it_only_when_set(self) -> None:
        # So no other bottle's document (or fingerprint) changes.
        self.assertNotIn("preserve_container", bottle_document(ManifestBottle.from_dict("b", {})))
        self.assertTrue(bottle_document(ManifestBottle.from_dict(
            "b", {"preserve_container": True}))["preserve_container"])

    def test_resume_fresh_reaches_the_host_controller(self) -> None:
        request = LaunchRequest(agent="a", fresh_guest=True)
        self.assertTrue(LaunchRequest.from_json(request.to_json(), ROLE_CLI).fresh_guest)


class TestWhenTheGuestIsKept(_Root):
    def test_only_a_preserved_bottle_kept_for_resume(self) -> None:
        self.assertFalse(keeps_guest("b1", True))  # it finished: it goes
        bottle_state.mark_preserved("b1")  # it was stopped
        self.assertTrue(keeps_guest("b1", True))
        self.assertFalse(keeps_guest("b1", False))  # its home is kept, not its guest

    def test_resume_reuses_it_unless_fresh(self) -> None:
        self.assertTrue(reuses_guest(_plan()))
        self.assertFalse(reuses_guest(_plan(fresh=True)))
        self.assertFalse(reuses_guest(_plan(preserve=False)))

    def test_cleanup_leaves_a_kept_guest(self) -> None:
        bottle_state.mark_preserved("kept")
        self.assertEqual({"bot-bottle-kept"},
                         kept_guests(["bot-bottle-kept", "bot-bottle-gone", "bot-bottle-mac-db"]))

    def test_an_adopted_bottle_remembers_it(self) -> None:
        record = adoption.AdoptionRecord(container="c", registry_id="r", preserve_container=True)
        adoption.write("b1", record)
        loaded = adoption.read("b1")
        assert loaded is not None
        self.assertTrue(loaded.preserve_container)


def _done(code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", "boom" if code else "")


class TestMacos(_Root):
    def test_teardown_stops_a_kept_guest_and_removes_any_other(self) -> None:
        from bot_bottle.backend.macos_container import launch
        bottle_state.mark_preserved("b1")
        with mock.patch.object(launch.container_mod, "stop_container") as stop, \
                mock.patch.object(launch.container_mod, "force_remove_container") as remove:
            launch._stop_or_remove("bot-bottle-b1", "b1", True)  # pylint: disable=protected-access
            launch._stop_or_remove("bot-bottle-b2", "b2", True)  # pylint: disable=protected-access
        stop.assert_called_once_with("bot-bottle-b1")
        remove.assert_called_once_with("bot-bottle-b2")

    def test_a_kept_guest_that_wont_start_is_replaced(self) -> None:
        from bot_bottle.backend.macos_container import launch
        plan = cast(Any, SimpleNamespace(container_name="bot-bottle-b1", slug="b1", image="i"))
        cm = launch.container_mod
        with mock.patch.object(cm, "container_is_running", return_value=False), \
                mock.patch.object(cm, "container_image_digest", return_value="d"), \
                mock.patch.object(cm, "image_digest", return_value="d"), \
                mock.patch.object(cm, "start_container", return_value="network gone"), \
                mock.patch.object(cm, "force_remove_container") as remove:
            self.assertFalse(launch._restart_agent(plan))  # pylint: disable=protected-access
        remove.assert_called_once_with("bot-bottle-b1")


class TestDocker(_Root):
    def test_the_identity_token_goes_with_each_exec_off_ps(self) -> None:
        from bot_bottle.backend.docker.bottle import DockerBottle
        bottle = DockerBottle("c", lambda: None, None,
                              exec_env={"HTTPS_PROXY": "http://bottle:tok@gw:9099"})
        argv = bottle.agent_argv(["--x"], tty=False)
        self.assertEqual(["docker", "exec", "-e", "HTTPS_PROXY", "c"], argv[:5])
        self.assertNotIn("tok", " ".join(argv))
        self.assertEqual({"HTTPS_PROXY": "http://bottle:tok@gw:9099"},
                         bottle.agent_command(["--x"])[1])
        with mock.patch("bot_bottle.backend.docker.bottle.run_script") as run:
            bottle.exec("true")
        self.assertIn("-e", run.call_args.args[0])
        self.assertEqual("http://bottle:tok@gw:9099", run.call_args.kwargs["env"]["HTTPS_PROXY"])

    def test_teardown_keeps_a_guest_off_its_network_or_composes_down(self) -> None:
        from bot_bottle.backend.docker import launch
        bottle_state.mark_preserved("b1")
        with mock.patch.object(launch.docker_mod, "stop_container") as stop, \
                mock.patch.object(launch.docker_mod, "run_docker") as docker, \
                mock.patch.object(launch, "compose_down") as down:
            # pylint: disable=protected-access
            launch._stop_or_down("p1", Path("f"), "bot-bottle-b1", network="n1", slug="b1",
                                 preserve_container=True)
            launch._stop_or_down("p2", Path("f"), "bot-bottle-b2", network="n2", slug="b2",
                                 preserve_container=True)
        stop.assert_called_once_with("bot-bottle-b1")
        self.assertEqual(["docker", "network", "disconnect", "-f", "n1", "bot-bottle-b1"],
                         docker.call_args.args[0])
        down.assert_called_once_with("p2", Path("f"))

    def test_a_kept_guest_is_rewired_to_this_runs_network(self) -> None:
        from bot_bottle.backend.docker import util
        calls: list[list[str]] = []

        def docker(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            out = "old-net " if argv[1] == "inspect" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        with mock.patch.object(util, "run_docker", side_effect=docker):
            self.assertEqual("", util.rewire_and_start("c", "new-net", "10.0.0.3"))
        self.assertEqual([["docker", "network", "disconnect", "-f", "old-net", "c"],
                          ["docker", "network", "connect", "--ip", "10.0.0.3", "new-net", "c"],
                          ["docker", "start", "c"]], calls[1:])


class TestFirecracker(_Root):
    def test_the_guest_disk_is_reused_unless_fresh(self) -> None:
        from bot_bottle.backend.firecracker import util

        def make(_base: Path, out: Path) -> None:
            out.write_bytes(b"disk")

        infra = cast(Any, object())
        with mock.patch.object(util, "make_bottle_rootfs", side_effect=make) as made, \
                mock.patch.object(util.subprocess, "run", return_value=_done()), \
                mock.patch.object(util, "inject_guest_boot_ext4") as inject:
            path, kept = util.kept_guest_image("b1", Path("/base"), infra, fresh=False)
            self.assertEqual((bottle_state.bottle_state_dir("b1") / "guest.ext4", False),
                             (path, kept))
            self.assertEqual((path, True), util.kept_guest_image("b1", Path("/base"), infra,
                                                                  fresh=False))
            # A kept guest gets this host's init and dropbear again.
            inject.assert_called_once_with(path, infra)
            self.assertEqual((path, False), util.kept_guest_image("b1", Path("/base"), infra,
                                                                   fresh=True))
        self.assertEqual(2, made.call_count)


class TestRestartingTheKeptGuest(_Root):
    def test_macos_starts_it_again_and_notes_an_older_image(self) -> None:
        from bot_bottle.backend.macos_container import launch, util
        plan = cast(Any, SimpleNamespace(container_name="bot-bottle-b1", slug="b1", image="i:2"))
        cm = launch.container_mod
        with mock.patch.object(cm, "container_is_running", return_value=True), \
                mock.patch.object(cm, "stop_container") as stop, \
                mock.patch.object(cm, "container_image_digest", return_value="old"), \
                mock.patch.object(cm, "image_digest", return_value="new"), \
                mock.patch.object(cm, "start_container", return_value=""), \
                mock.patch.object(launch, "info") as info:
            self.assertTrue(launch._restart_agent(plan))  # pylint: disable=protected-access
        stop.assert_called_once_with("bot-bottle-b1")  # left running by a run that ended badly
        self.assertIn("resume --fresh b1", str(info.call_args_list))
        with mock.patch.object(util.subprocess, "run",
                               side_effect=[_done(), _done(), _done(1)]) as run:
            util.stop_container("c")
            self.assertEqual("", util.start_container("c"))
            self.assertEqual("boom", util.start_container("c"))
        self.assertEqual(["container", "stop", "--time", "2", "c"], run.call_args_list[0].args[0])

    def test_docker_starts_it_on_this_runs_network_or_replaces_it(self) -> None:
        from bot_bottle.backend.docker import launch
        plan = cast(Any, SimpleNamespace(container_name="bot-bottle-b1", slug="b1", image="i:2"))
        dm = launch.docker_mod
        for why, started in (("", True), ("no such network", False)):
            with self.subTest(why=why), \
                    mock.patch.object(dm, "stop_container"), \
                    mock.patch.object(dm, "container_image_id", return_value="old"), \
                    mock.patch.object(dm, "image_id", return_value="new"), \
                    mock.patch.object(dm, "rewire_and_start", return_value=why) as rewire, \
                    mock.patch.object(dm, "force_remove_container") as remove:
                self.assertEqual(started, launch._restart_agent(plan, "n1", "10.0.0.3"))  # pylint: disable=protected-access
            rewire.assert_called_once_with("bot-bottle-b1", "n1", "10.0.0.3")
            self.assertEqual(not started, remove.called)

    def test_docker_reads_a_containers_env_and_image(self) -> None:
        from bot_bottle.backend.docker import util

        def docker(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            out = '["ENV_VAR_SECRET=s", "PATH=/bin"]' if "Config.Env" in argv[3] else "sha256:x"
            return subprocess.CompletedProcess(argv, 0, out, "")

        with mock.patch.object(util, "run_docker", side_effect=docker):
            self.assertEqual({"ENV_VAR_SECRET": "s", "PATH": "/bin"}, util.container_env("c"))
            self.assertEqual("sha256:x", util.container_image_id("c"))
        with mock.patch.object(util, "run_docker", return_value=_done(1)):
            self.assertEqual({}, util.container_env("c"))
            self.assertEqual("", util.container_image_id("c"))
            self.assertEqual("boom", util.rewire_and_start("c", "n", "10.0.0.3"))
        with mock.patch.object(util, "run_docker") as run:
            util.stop_container("c")
        self.assertEqual(["docker", "stop", "--time", "2", "c"], run.call_args.args[0])


class TestListingAndCleanup(_Root):
    def test_firecracker_says_its_guest_disk_is_kept(self) -> None:
        from bot_bottle.backend.firecracker.backend import FirecrackerBottleBackend
        from bot_bottle.backend.firecracker.util import guest_image_path
        from bot_bottle.contract.hostctl import Settings
        backend = FirecrackerBottleBackend(Settings())
        self.assertFalse(backend.kept_guest("b1"))
        guest_image_path("b1").parent.mkdir(parents=True)
        guest_image_path("b1").write_bytes(b"")
        self.assertTrue(backend.kept_guest("b1"))

    def test_macos_cleanup_leaves_a_kept_guest(self) -> None:
        from bot_bottle.backend.macos_container import cleanup
        bottle_state.mark_preserved("kept")
        with mock.patch.object(cleanup.container_mod, "require_container"), \
                mock.patch.object(cleanup, "_list_prefixed_containers",
                                  return_value=["bot-bottle-kept", "bot-bottle-gone"]), \
                mock.patch.object(cleanup, "_list_prefixed_networks", return_value=[]), \
                mock.patch.object(cleanup.container_mod, "list_volumes", return_value=[]):
            self.assertEqual(("bot-bottle-gone",), cleanup.prepare_cleanup().containers)

    def test_the_listing_leaves_kept_blank_when_it_cant_tell(self) -> None:
        from bot_bottle.cli.commands import bottled_agents
        bottle_state.mark_preserved("b1")
        metadata = cast(Any, SimpleNamespace(identity="b1", backend="docker"))
        with mock.patch.object(bottled_agents, "settings_from_env"), \
                mock.patch.object(bottled_agents, "get_bottle_backend",
                                  side_effect=RuntimeError("down")):
            self.assertEqual("", bottled_agents._kept_guest(metadata))  # pylint: disable=protected-access

    def test_the_preview_shows_it(self) -> None:
        from bot_bottle.manifest import Manifest
        from bot_bottle.manifest.agent import ManifestAgent
        from bot_bottle.manifest.preview import manifest_to_yaml
        manifest = Manifest(agent=ManifestAgent(),
                            bottle=ManifestBottle(preserve_container=True))
        self.assertIn("preserve_container: true", manifest_to_yaml(manifest))


if __name__ == "__main__":
    unittest.main()
