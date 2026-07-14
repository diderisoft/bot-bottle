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
    def test_dns_server_prefers_direct_host_ipv4_resolver(self):
        scutil = util.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="""
resolver #1
  nameserver[0] : 100.100.100.100
  reach    : 0x00000003 (Reachable,Transient Connection)

resolver #2
  nameserver[0] : 2600:4041:5c43:b900::1
  nameserver[1] : 192.168.1.1
  reach    : 0x00020002 (Reachable,Directly Reachable Address)
""",
            stderr="",
        )
        with patch.object(util.os, "environ", {}), \
             patch.object(util.platform, "system", return_value="Darwin"), \
             patch.object(util.subprocess, "run", return_value=scutil):
            self.assertEqual("192.168.1.1", util.dns_server())

    def test_build_image(self):
        status = util.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"status":{"state":"running"},'
                '"configuration":{"dns":{"nameservers":["9.9.9.9"]}}}]'
            ),
            stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=status) as run, \
             patch.object(util.os, "environ", {
                 "BOT_BOTTLE_MACOS_CONTAINER_DNS": "9.9.9.9",
             }):
            util.build_image("bot-bottle-agent:latest", "/repo", dockerfile="/repo/Dockerfile")
        self.assertEqual(
            [
                "container", "build", "-t", "bot-bottle-agent:latest",
                "--dns", "9.9.9.9", "-f", "/repo/Dockerfile", "/repo",
            ],
            run.call_args_list[-1].args[0],
        )
        self.assertTrue(run.call_args_list[-1].kwargs["check"])

    def test_build_image_anchors_relative_dockerfile_to_context(self):
        status = util.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"status":{"state":"running"},'
                '"configuration":{"dns":{"nameservers":["9.9.9.9"]}}}]'
            ),
            stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=status) as run, \
             patch.object(util.os, "environ", {
                 "BOT_BOTTLE_MACOS_CONTAINER_DNS": "9.9.9.9",
             }):
            util.build_image(
                "bot-bottle-gateway:latest",
                "/repo",
                dockerfile="Dockerfile.gateway",
            )
        self.assertEqual(
            [
                "container", "build", "-t", "bot-bottle-gateway:latest",
                "--dns", "9.9.9.9", "-f", "/repo/Dockerfile.gateway", "/repo",
            ],
            run.call_args_list[-1].args[0],
        )

    def test_commit_container_execs_tar_and_builds_image(self):
        # stderr is bytes because subprocess.run uses stderr=PIPE without text=True
        completed = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b"",
        )
        dockerfile_text = ""

        def fake_build_image(image_tag: str, context: str, *, dockerfile: str = "") -> None:
            nonlocal dockerfile_text
            with open(dockerfile, encoding="utf-8") as f:
                dockerfile_text = f.read()

        with patch.object(util.subprocess, "run", return_value=completed) as run, \
             patch.object(util, "build_image", side_effect=fake_build_image) as build_image, \
             patch.object(util, "info"):
            util.commit_container(
                "bot-bottle-dev-abc12",
                "bot-bottle-committed-dev-abc12:latest",
            )

        argv = run.call_args.args[0]
        self.assertEqual("container", argv[0])
        self.assertEqual("exec", argv[1])
        self.assertIn("bot-bottle-dev-abc12", argv)
        self.assertIn("tar", argv)
        self.assertIn("--directory=/", argv)
        build_image.assert_called_once()
        self.assertEqual(
            "bot-bottle-committed-dev-abc12:latest",
            build_image.call_args.args[0],
        )
        self.assertIn("ADD rootfs.tar /\n", dockerfile_text)
        self.assertIn("USER node\n", dockerfile_text)
        self.assertIn("WORKDIR /home/node\n", dockerfile_text)

    def test_commit_container_dies_on_exec_tar_failure(self):
        failed = util.subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"No such container",
        )
        with patch.object(util.subprocess, "run", return_value=failed), \
             patch.object(util, "die", side_effect=SystemExit("die")) as die:
            with self.assertRaises(SystemExit):
                util.commit_container("missing-container", "some:tag")

        die.assert_called_once()
        self.assertIn("missing-container", die.call_args.args[0])

    def test_build_image_restarts_builder_when_dns_mismatches(self):
        status = util.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"status":{"state":"running"},'
                '"configuration":{"dns":{"nameservers":[]}}}]'
            ),
            stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=status) as run, \
             patch.object(util.os, "environ", {
                 "BOT_BOTTLE_MACOS_CONTAINER_DNS": "9.9.9.9",
             }):
            util.build_image("bot-bottle-agent:latest", "/repo")
        calls = [c.args[0] for c in run.call_args_list]
        self.assertIn(["container", "builder", "stop"], calls)
        self.assertIn(
            ["container", "builder", "start", "--dns", "9.9.9.9"],
            calls,
        )
        self.assertEqual(
            [
                "container", "build", "-t", "bot-bottle-agent:latest",
                "--dns", "9.9.9.9", "/repo",
            ],
            calls[-1],
        )

    def test_build_image_leaves_working_builder_with_different_dns_alone(self):
        status = util.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"status":{"state":"running"},'
                '"configuration":{"dns":{"nameservers":["8.8.8.8"]}}}]'
            ),
            stderr="",
        )
        probe = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        build = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch.object(util, "dns_server", return_value="192.168.1.1"), \
             patch.object(util.os, "environ", {}), \
             patch.object(util.subprocess, "run", side_effect=[status, probe, build]) as run:
            util.build_image("bot-bottle-agent:latest", "/repo")
        calls = [c.args[0] for c in run.call_args_list]
        self.assertNotIn(["container", "builder", "stop"], calls)
        self.assertNotIn(
            ["container", "builder", "start", "--dns", "192.168.1.1"],
            calls,
        )

    def test_build_image_restarts_builder_when_dns_probe_fails(self):
        status = util.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '[{"status":{"state":"running"},'
                '"configuration":{"dns":{"nameservers":["8.8.8.8"]}}}]'
            ),
            stderr="",
        )
        failed_probe = util.subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="",
        )
        ok = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch.object(util, "dns_server", return_value="192.168.1.1"), \
             patch.object(util.os, "environ", {}), \
             patch.object(
                 util.subprocess,
                 "run",
                 side_effect=[status, failed_probe, ok, ok, ok],
             ) as run:
            util.build_image("bot-bottle-agent:latest", "/repo")
        calls = [c.args[0] for c in run.call_args_list]
        self.assertIn(["container", "builder", "stop"], calls)
        self.assertIn(
            ["container", "builder", "start", "--dns", "192.168.1.1"],
            calls,
        )

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
