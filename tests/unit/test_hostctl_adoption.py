"""Unit: a restarted host controller finishes bottles it didn't launch (#679).

At launch the holder writes an adoption record; when an adopted session ends,
the controller saves the agent's work, tears the bottle down through its
backend and revokes its deploy keys, all from that record."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle import bottle_state
from bot_bottle.backend import adoption
from bot_bottle.backend.docker import launch as docker_launch
from bot_bottle.backend.firecracker import cleanup as fc_cleanup
from bot_bottle.backend.firecracker import firecracker_vm
from bot_bottle.backend.firecracker import launch as fc_launch
from bot_bottle.backend.macos_container import launch as mac_launch
from bot_bottle.contract.hostctl import Settings
from bot_bottle.control.hostctl import sessions
from bot_bottle.control.hostctl.bottles import BottleService
from bot_bottle.git_gate.provision import DeployKeyRef
from . import use_bottle_root

_KEY = DeployKeyRef(name="app", forge_token_env="GITEA_TOKEN", api_url="",
                    upstream_host="git.example", upstream_path="o/app.git")
_RECORD = adoption.AdoptionRecord(
    container="bot-bottle-b1", registry_id="reg-1",
    workspace=adoption.AdoptedWorkspace(enabled=True, has_host_git_dir=True,
                                        guest_path="/home/node/workspace"),
    deploy_keys=(_KEY,),
)


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbadopt.")))
        self.root = root
        self.addCleanup(use_bottle_root(root))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))


def _bottled(identity: str) -> None:
    bottle_state.write_metadata(bottle_state.BottleMetadata(
        identity=identity, agent_name="demo", cwd="", copy_cwd=False, started_at="t"))


class TestRecord(_Root):
    def test_round_trips_with_its_bottled_agent_and_holds_no_secrets(self) -> None:
        _bottled("b1")
        self.assertTrue(adoption.write("b1", _RECORD))
        self.assertEqual(_RECORD, adoption.read("b1"))
        stored = json.loads(bottle_state.store().adoption("b1"))
        self.assertNotIn("token", stored["deploy_keys"][0].get("value", ""))
        self.assertFalse(bottle_state.bottle_state_dir("b1").exists())

    def test_missing_or_without_a_bottled_agent_is_none(self) -> None:
        self.assertIsNone(adoption.read("nope"))
        self.assertFalse(adoption.write("nope", _RECORD))
        self.assertIsNone(adoption.read("nope"))
        _bottled("b2")
        self.assertIsNone(adoption.read("b2"))

    def test_it_goes_with_its_bottled_agent(self) -> None:
        _bottled("b3")
        adoption.write("b3", _RECORD)
        bottle_state.remove_bottled_agent("b3")
        self.assertIsNone(adoption.read("b3"))

    def test_record_for_takes_the_plans_workspace_and_gitea_repos(self) -> None:
        entry = SimpleNamespace(Name="app", UpstreamHost="git.example", UpstreamPath="o/app.git",
                                Key=SimpleNamespace(provider="gitea", forge_token_env="GITEA_TOKEN",
                                                    api_url=""))
        static = SimpleNamespace(Name="lib", UpstreamHost="h", UpstreamPath="p",
                                 Key=SimpleNamespace(provider="static", forge_token_env="",
                                                     api_url=""))
        plan = SimpleNamespace(
            workspace_plan=SimpleNamespace(enabled=True, has_host_git_dir=True,
                                           guest_path="/home/node/workspace"),
            manifest=SimpleNamespace(bottle=SimpleNamespace(git=(entry, static))))
        bottle = SimpleNamespace(name="bot-bottle-b1", registry_id="reg-1")
        self.assertEqual(_RECORD, adoption.record_for(cast(Any, plan), cast(Any, bottle)))


class TestFinishAdopted(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[str] = []
        self.bottle = mock.Mock(name="bottle")
        self.bottle.exec.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        @contextlib.contextmanager
        def adopted_bottle(bottle_id: str, record: Any) -> Generator[Any, None, None]:
            self.events.append(f"open {bottle_id} {record.container}")
            try:
                yield self.bottle
            finally:
                self.events.append("teardown")

        self.backend = mock.MagicMock()
        self.backend.adopted_bottle.side_effect = adopted_bottle
        self.service = BottleService(mock.MagicMock(), backend_for=lambda _s: self.backend,
                                     recorded_infra=lambda _b: None, audit=mock.MagicMock(),
                                     secrets_file=self.root / "secrets")
        self.record = sessions.SessionRecord(id="b1", kind="bottle",
                                             meta={"backend": "docker", "activation": "reg-1"})

    def _finish(self) -> None:
        self.service._finish(self.record, sessions.ExitStatus(0))  # pyright: ignore[reportPrivateUsage]

    def test_saves_work_tears_down_and_revokes_keys(self) -> None:
        _bottled("b1")
        adoption.write("b1", _RECORD)
        with mock.patch("bot_bottle.control.hostctl.bottles._save_work") as save, \
             mock.patch("bot_bottle.git_gate.provision.revoke_deploy_keys") as revoke:
            self._finish()
        self.assertEqual(["open b1 bot-bottle-b1", "teardown"], self.events)
        save.assert_called_once_with(self.bottle, _RECORD.workspace, "b1")
        self.assertEqual((_KEY,), revoke.call_args.args[0])
        # Keys come from the secrets file only; request secrets aren't kept.
        self.assertEqual("FileSecrets", type(revoke.call_args.args[2]).__name__)

    def test_without_a_record_it_leaves_the_bottle_for_cleanup(self) -> None:
        self._finish()
        self.backend.adopted_bottle.assert_not_called()
        self.assertIn("run `bot-bottle cleanup bots`", self.err.getvalue())

    def test_a_failed_teardown_still_revokes_the_keys(self) -> None:
        _bottled("b1")
        adoption.write("b1", _RECORD)
        self.backend.adopted_bottle.side_effect = RuntimeError("container gone")
        with mock.patch("bot_bottle.git_gate.provision.revoke_deploy_keys") as revoke:
            self._finish()
        revoke.assert_called_once()
        self.assertIn("teardown failed", self.err.getvalue())

    def test_a_key_that_cant_be_revoked_is_reported(self) -> None:
        _bottled("b1")
        adoption.write("b1", _RECORD)
        with mock.patch("bot_bottle.control.hostctl.bottles._save_work"), \
             mock.patch("bot_bottle.git_gate.provision.revoke_deploy_keys",
                        side_effect=RuntimeError("GITEA_TOKEN is not set")):
            self._finish()  # doesn't raise
        self.assertIn("revoke them by hand", self.err.getvalue())

    def test_the_slot_is_released_and_state_settled_either_way(self) -> None:
        release = mock.Mock()
        self.service._adopted["b1"] = release  # pyright: ignore[reportPrivateUsage]
        self.backend.adopted_bottle.side_effect = RuntimeError("boom")
        _bottled("b1")
        adoption.write("b1", _RECORD)
        with mock.patch("bot_bottle.git_gate.provision.revoke_deploy_keys"), \
             mock.patch("bot_bottle.control.hostctl.bottles.settle_state") as settle:
            self._finish()
        release.assert_called_once_with()
        settle.assert_called_once_with("b1")


    def _exit_audit(self) -> dict[str, Any]:
        audit = cast(mock.MagicMock, self.service.audit)
        return audit.bottled_agent_exit.call_args.kwargs

    def _stop(self, code: int) -> None:
        """A requested teardown of the re-adopted bottle, its agent exiting
        with `code` when the session is killed."""
        sessions_ = cast(mock.MagicMock, self.service._sessions)  # pyright: ignore[reportPrivateUsage]
        sessions_.record.return_value = self.record

        def killed(_bottle_id: str) -> None:
            self.service._finish(  # pyright: ignore[reportPrivateUsage]
                self.record, sessions.ExitStatus(code))
        sessions_.kill.side_effect = killed
        sessions_.wait_exit.return_value = sessions.ExitStatus(code)
        self.service.teardown("b1")

    def test_a_requested_stop_is_audited_as_a_stop(self) -> None:
        self._stop(255)
        self.assertEqual(255, self._exit_audit()["exit_code"])
        self.assertTrue(self._exit_audit()["stop_requested"])

    def test_an_unrequested_exit_is_still_a_crash(self) -> None:
        self.service._finish(self.record, sessions.ExitStatus(255))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(self._exit_audit()["stop_requested"])

    def test_the_request_is_spent_by_the_exit_it_stopped(self) -> None:
        self._stop(255)
        self.service._finish(self.record, sessions.ExitStatus(255))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(self._exit_audit()["stop_requested"])

    def test_a_failed_teardown_forgets_the_request(self) -> None:
        sessions_ = cast(mock.MagicMock, self.service._sessions)  # pyright: ignore[reportPrivateUsage]
        sessions_.record.return_value = self.record
        sessions_.kill.side_effect = RuntimeError("no session")
        with self.assertRaises(RuntimeError):
            self.service.teardown("b1")
        self.service._finish(self.record, sessions.ExitStatus(255))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(self._exit_audit()["stop_requested"])


class TestBackendTeardowns(_Root):
    """Each backend undoes what its launch set up, in its launch's order."""

    def test_docker_composes_down_then_deregisters(self) -> None:
        order = mock.Mock()
        with mock.patch.object(docker_launch, "compose_down", order.down), \
             mock.patch.object(docker_launch, "deprovision_consolidated", order.dereg), \
             mock.patch.object(docker_launch, "DockerInfraService"), \
             mock.patch.object(docker_launch, "resolve_teardown_timeout", return_value=1):
            with docker_launch.adopted("b1", _RECORD, Settings()) as bottle:
                self.assertEqual("bot-bottle-b1", bottle.name)
        self.assertEqual(["down", "dereg"], [c[0] for c in order.mock_calls])
        self.assertEqual(docker_launch.compose_project_name("b1"), order.down.call_args.args[0])
        self.assertEqual("reg-1", order.dereg.call_args.args[0])

    def test_macos_removes_the_container_then_deregisters(self) -> None:
        order = mock.Mock()
        with mock.patch.object(mac_launch.container_mod, "force_remove_container", order.rm), \
             mock.patch.object(mac_launch, "deprovision_consolidated", order.dereg), \
             mock.patch.object(mac_launch, "MacosInfraService"), \
             mock.patch.object(mac_launch, "resolve_teardown_timeout", return_value=1):
            with mac_launch.adopted("b1", _RECORD, Settings()):
                pass
        self.assertEqual([mock.call.rm("bot-bottle-b1"), mock.call.dereg("reg-1", service=mock.ANY,
                                                                        timeout=1)],
                         order.mock_calls)

    def test_no_registry_id_means_nothing_to_deregister(self) -> None:
        record = adoption.AdoptionRecord(container="c", registry_id="")
        with mock.patch.object(mac_launch.container_mod, "force_remove_container"), \
             mock.patch.object(mac_launch, "deprovision_consolidated") as dereg:
            with mac_launch.adopted("b1", record, Settings()):
                pass
        dereg.assert_not_called()

    def test_firecracker_stops_the_vm_removes_the_run_dir_then_deregisters(self) -> None:
        run_dir = self.root / "cache" / "run" / "b1"
        run_dir.mkdir(parents=True)
        (run_dir / "config.json").write_text(json.dumps(
            {"boot-source": {"boot_args": "console=ttyS0 ip=10.243.0.9::10.243.0.8:"
                                          "255.255.255.254::eth0:off bb_pubkey=x"}}))
        order = mock.Mock()
        with mock.patch.object(fc_launch.util, "cache_dir", return_value=self.root / "cache"), \
             mock.patch("bot_bottle.backend.firecracker.cleanup.stop_vmms", order.stop), \
             mock.patch.object(fc_launch, "deprovision_consolidated", order.dereg), \
             mock.patch.object(fc_launch, "FirecrackerInfraService"), \
             mock.patch.object(fc_launch, "resolve_teardown_timeout", return_value=1):
            with fc_launch.adopted("b1", _RECORD, Settings()) as bottle:
                self.assertEqual("10.243.0.9", bottle._guest_ip)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([mock.call.stop(run_dir), mock.call.dereg("reg-1", service=mock.ANY,
                                                                   timeout=1)],
                         order.mock_calls)
        self.assertFalse(run_dir.exists())


class TestFirecrackerHelpers(_Root):
    def test_guest_ip_of(self) -> None:
        self.assertEqual("", firecracker_vm.guest_ip_of(self.root))
        (self.root / "config.json").write_text(json.dumps(
            {"boot-source": {"boot_args": "ip=10.0.0.3::10.0.0.2:255.255.255.254::eth0:off"}}))
        self.assertEqual("10.0.0.3", firecracker_vm.guest_ip_of(self.root))

    def test_stop_vmms_signals_only_this_run_dirs_vm(self) -> None:
        run_dir = self.root / "run" / "b1"
        args = {11: ("firecracker", "--config-file", str(run_dir / "config.json")),
                12: ("firecracker", "--config-file", str(self.root / "run" / "b2" / "config.json"))}
        def pidfd_open(pid: int) -> int:
            return 100 + pid

        def exited(readable: list[int], *_rest: object) -> tuple[list[int], list[int], list[int]]:
            return readable, [], []

        with mock.patch.object(fc_cleanup, "_firecracker_pids", return_value=[11, 12]), \
             mock.patch.object(fc_cleanup, "_process_args", side_effect=args.get), \
             mock.patch.object(fc_cleanup.os, "pidfd_open", side_effect=pidfd_open), \
             mock.patch.object(fc_cleanup.os, "close"), \
             mock.patch.object(fc_cleanup.signal, "pidfd_send_signal") as send, \
             mock.patch.object(fc_cleanup.select, "select", side_effect=exited):
            fc_cleanup.stop_vmms(run_dir)
        self.assertEqual([mock.call(111, fc_cleanup.signal.SIGTERM)], send.call_args_list)


if __name__ == "__main__":
    unittest.main()
