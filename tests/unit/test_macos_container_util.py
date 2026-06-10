"""Unit: Apple Container utility helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.backend.macos_container import util


class TestMacosContainerAvailability(unittest.TestCase):
    def test_available_only_on_macos_with_container(self):
        with patch.object(util.platform, "system", return_value="Darwin"), \
             patch.object(util.shutil, "which", return_value="/usr/local/bin/container"):
            self.assertTrue(util.is_available())

    def test_not_available_off_macos(self):
        with patch.object(util.platform, "system", return_value="Linux"), \
             patch.object(util.shutil, "which", return_value="/usr/local/bin/container"):
            self.assertFalse(util.is_available())

    def test_require_container_dies_when_missing(self):
        with patch.object(util.platform, "system", return_value="Darwin"), \
             patch.object(util.shutil, "which", return_value=None), \
             patch.object(util, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                util.require_container()


class TestMacosContainerCommands(unittest.TestCase):
    def test_build_image(self):
        with patch.object(util.subprocess, "run") as run:
            util.build_image("bot-bottle-agent:latest", "/repo", dockerfile="/repo/Dockerfile")
        self.assertEqual(
            [
                "container", "build", "-t", "bot-bottle-agent:latest",
                "-f", "/repo/Dockerfile", "/repo",
            ],
            run.call_args.args[0],
        )
        self.assertTrue(run.call_args.kwargs["check"])

    def test_container_exists_parses_quiet_list(self):
        completed = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="bot-bottle-a\nother\n", stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=completed):
            self.assertTrue(util.container_exists("bot-bottle-a"))
            self.assertFalse(util.container_exists("bot-bottle-b"))

    def test_image_id_reads_json_digest(self):
        completed = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"digest":"sha256:abc"}', stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=completed):
            self.assertEqual("sha256:abc", util.image_id("demo:latest"))

    def test_container_ipv4_on_network_reads_inspect_json(self):
        payload = """[{
          "status": {
            "networks": [
              {
                "network": "bot-bottle-net-demo",
                "ipv4Address": "192.168.128.2/24"
              }
            ]
          }
        }]"""
        completed = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=completed):
            self.assertEqual(
                "192.168.128.2",
                util.container_ipv4_on_network(
                    "bot-bottle-sidecars-demo",
                    "bot-bottle-net-demo",
                ),
            )


if __name__ == "__main__":
    unittest.main()
