"""Unit: `bot-bottle uninstall`, and finding what backends leave on the host."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.backend.docker import host_resources as docker_res
from bot_bottle.backend.host_resources import HostResource, is_bot_bottle_image
from bot_bottle.backend.macos_container import host_resources as mac_res
from bot_bottle.cli.commands import uninstall


class TestImageNames(unittest.TestCase):
    def test_bot_bottle_images_on_any_registry(self) -> None:
        for ref in ("bot-bottle-claude:latest", "docker.io/library/bot-bottle-gateway:src-1",
                    "gitea.dideric.is/didericis/bot-bottle-pi@sha256:ab"):
            self.assertTrue(is_bot_bottle_image(ref, purge=False), ref)

    def test_base_images_and_frozen_bottles(self) -> None:
        for ref in ("docker.io/library/python:3.13", "node:22", "quay.io/x/bottle:1"):
            self.assertFalse(is_bot_bottle_image(ref, purge=True), ref)
        self.assertFalse(is_bot_bottle_image("bot-bottle-committed-test2:latest", purge=False))
        self.assertTrue(is_bot_bottle_image("bot-bottle-committed-test2:latest", purge=True))


def _completed(out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, out, "")


class TestMacosResources(unittest.TestCase):
    def test_lists_only_bot_bottle_containers_networks_volumes_and_images(self) -> None:
        listings = {
            "list": [{"id": "bot-bottle-mac-infra"}, {"id": "buildkit"}],
            "network": [{"configuration": {"name": "bot-bottle-mac-gateway"}},
                        {"configuration": {"name": "default"}}],
            "volume": [{"configuration": {"name": "bot-bottle-mac-db"}}],
            "image": [{"configuration": {"name": "bot-bottle-claude:latest"}},
                      {"configuration": {"name": "docker.io/library/python:3.13"}},
                      {"configuration": {"name": "bot-bottle-committed-x:latest"}}],
        }

        def run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            return _completed(json.dumps(listings[argv[1]]))
        with mock.patch("bot_bottle.backend.host_resources.subprocess.run", side_effect=run):
            found = [r.what for r in mac_res.list_resources(purge=False)]
        self.assertEqual(["container bot-bottle-mac-infra", "network bot-bottle-mac-gateway",
                          "volume bot-bottle-mac-db", "image bot-bottle-claude:latest"], found)

    def test_remove_runs_the_matching_delete(self) -> None:
        def run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            volumes = [{"configuration": {"name": "bot-bottle-mac-db"}}]
            return _completed(json.dumps(volumes if argv[1] == "volume" else []))
        with mock.patch("bot_bottle.backend.host_resources.subprocess.run", side_effect=run):
            [volume] = mac_res.list_resources(purge=False)  # pylint: disable=unbalanced-tuple-unpacking
        with mock.patch("bot_bottle.backend.host_resources.subprocess.run",
                        return_value=_completed("")) as run:
            self.assertTrue(volume.remove())
        self.assertEqual(["container", "volume", "delete", "bot-bottle-mac-db"],
                         run.call_args.args[0])


class TestDockerResources(unittest.TestCase):
    def test_lists_containers_networks_and_images(self) -> None:
        listings = {"ps": "bot-bottle-demo-agent\nunrelated\n",
                    "network": "bot-bottle-gateway-egress\nbridge\n",
                    "image": "bot-bottle-gateway:latest\npython:3.13\n<none>:<none>\n"
                             "bot-bottle-x:<none>\n"}

        def run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            return _completed(listings[argv[1]])
        with mock.patch("bot_bottle.backend.host_resources.subprocess.run", side_effect=run):
            found = [r.what for r in docker_res.list_resources(purge=False)]
        self.assertEqual(["container bot-bottle-demo-agent", "network bot-bottle-gateway-egress",
                          "image bot-bottle-gateway:latest"], found)


class _Uninstall(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        (self.root / "agents").mkdir(parents=True)
        self.venv = self.root / "venv"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin" / "bot-bottle").write_text("#!/bin/sh\n")
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "bot-bottle").symlink_to(self.venv / "bin" / "bot-bottle")
        (self.home / ".cache" / "bot-bottle" / "artifacts").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.bin),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.enterContext(mock.patch.object(uninstall.Path, "home", return_value=self.home))
        self.enterContext(mock.patch.object(uninstall.sys, "prefix", str(self.venv)))
        self.removed: list[str] = []
        def remove_network() -> bool:
            self.removed.append("net")
            return True

        def host_resources(purge: bool) -> list[HostResource]:
            found = [HostResource("network bot-bottle-mac-gateway", remove_network)]
            if purge:
                found.append(HostResource("image bot-bottle-committed-x", lambda: True))
            return found
        self.backend = SimpleNamespace(
            name="macos-container", teardown=mock.Mock(return_value=0),
            infra_service=lambda: SimpleNamespace(stop=mock.Mock()),
            host_resources=host_resources)
        self.enterContext(mock.patch.object(uninstall, "_backends", return_value=[self.backend]))
        self.runner = self.enterContext(mock.patch.object(uninstall, "runner"))
        self.runner.running.return_value = None
        self.service = self.enterContext(mock.patch.object(uninstall, "service_unit"))
        self.service.uninstall.return_value = True

    def run_cmd(self, argv: list[str], answer: str = "y") -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(uninstall, "read_tty_line", return_value=answer), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = uninstall.cmd_uninstall(argv)
        return code, out.getvalue()


class TestUninstall(_Uninstall):
    def test_shows_the_plan_and_removes_everything_but_the_data(self) -> None:
        code, out = self.run_cmd([])
        self.assertEqual(0, code)
        self.assertLess(out.index("This removes"), out.index("Remove all of this?"))
        self.assertIn("macos-container: 1 network", out)
        self.assertIn(f"It keeps {self.root}", out)
        self.assertEqual(["net"], self.removed)
        self.backend.teardown.assert_called_once()
        self.assertFalse(self.venv.exists())
        self.assertFalse((self.bin / "bot-bottle").exists())
        self.assertFalse((self.home / ".cache" / "bot-bottle").exists())
        self.assertTrue((self.root / "agents").exists())  # kept

    def test_purge_also_deletes_the_data_and_frozen_bottles(self) -> None:
        code, out = self.run_cmd(["--purge"])
        self.assertEqual(0, code)
        self.assertIn("including your data", out)
        self.assertIn("macos-container: 1 network, 1 image", out)
        self.assertFalse(self.root.exists())

    def test_declining_removes_nothing(self) -> None:
        code, out = self.run_cmd([], answer="")
        self.assertEqual(1, code)
        self.assertIn("nothing removed", out)
        self.assertEqual([], self.removed)
        self.assertTrue(self.venv.exists())
        self.runner.stop.assert_not_called()

    def test_keep_backend_leaves_the_privileged_setup(self) -> None:
        self.run_cmd(["--keep-backend", "--yes"])
        self.backend.teardown.assert_not_called()

    def test_refuses_while_bottles_run_unless_forced(self) -> None:
        self.runner.running.return_value = object()
        self.runner.held_bottles.return_value = ["b1"]
        self.assertEqual(1, self.run_cmd(["--yes"])[0])
        self.assertTrue(self.venv.exists())
        self.service.uninstall.assert_not_called()
        self.assertEqual(0, self.run_cmd(["--yes", "--force"])[0])
        self.runner.stop.assert_called_with(force=True)


class TestPackageDetection(unittest.TestCase):
    def detect(self, prefix: str) -> Any:
        with mock.patch.object(uninstall.sys, "prefix", prefix), \
                mock.patch.dict(os.environ, {"BOT_BOTTLE_VENV": "/nowhere/venv"}):
            return uninstall._package()  # pylint: disable=protected-access

    def test_pipx_and_uv_are_removed_with_their_tools(self) -> None:
        self.assertEqual(["pipx", "uninstall", "bot-bottle"],
                         self.detect("/home/u/.local/pipx/venvs/bot-bottle").remove)
        self.assertEqual(["uv", "tool", "uninstall", "bot-bottle"],
                         self.detect("/home/u/.local/share/uv/tools/bot-bottle").remove)

    def test_a_checkout_is_left_alone(self) -> None:
        package = self.detect("/home/u/Code/bot-bottle/.venv")
        self.assertIsNone(package.remove)
        self.assertEqual((), package.paths)
        self.assertIn("leaves alone", package.how)


if __name__ == "__main__":
    unittest.main()
