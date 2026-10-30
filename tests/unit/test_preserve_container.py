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


if __name__ == "__main__":
    unittest.main()
