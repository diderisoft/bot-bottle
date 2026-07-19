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
                    "bot-bottle-demo",
                    "bot-bottle-net-demo",
                ),
            )

    def test_exec_container_as_root_selects_root_user(self):
        completed = util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch.object(util, "_run_container_op", return_value=completed) as run:
            util.exec_container_as_root("bot-bottle-demo", ["true"])

        run.assert_called_once_with([
            "container", "exec", "--user", "root", "bot-bottle-demo", "true",
        ])

    def test_exec_container_as_root_reports_failure(self):
        failed = util.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied\n",
        )
        with patch.object(util, "_run_container_op", return_value=failed), \
             patch.object(util, "die", side_effect=SystemExit("die")) as die:
            with self.assertRaises(SystemExit):
                util.exec_container_as_root("bot-bottle-demo", ["true"])

        die.assert_called_once_with(
            "container exec (root) in bot-bottle-demo failed: permission denied",
        )


def _completed(stdout: str, returncode: int = 0):
    return util.subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestInspectDigests(unittest.TestCase):
    """image_digest and container_image_digest must read the SAME field shape
    (a `descriptor.digest`) so a running container and its image are
    comparable. An asymmetry recreates the gateway on every launch."""

    _IMAGE = '{"configuration": {"descriptor": {"digest": "sha256:abc123"}}, "id": "abc123"}'
    _CONTAINER = '[{"configuration": {"image": {"descriptor": {"digest": "sha256:abc123"}}}}]'

    def test_image_and_container_digests_agree_for_the_same_image(self):
        with patch.object(util, "run_container_argv") as run:
            run.return_value = _completed(self._IMAGE)
            img = util.image_digest("bot-bottle-gateway:latest")
            run.return_value = _completed(self._CONTAINER)
            ctr = util.container_image_digest("bot-bottle-mac-gateway")
        self.assertEqual("abc123", img)
        self.assertEqual(img, ctr)

    def test_image_digest_never_falls_back_to_a_tag_or_id(self):
        """The old `id` fallback could yield a value container_image_digest
        can't produce, permanently mismatching. Missing descriptor → '' (don't
        churn), never a stray id/tag."""
        with patch.object(util, "run_container_argv") as run:
            run.return_value = _completed('{"id": "bot-bottle-gateway:latest"}')
            self.assertEqual("", util.image_digest("bot-bottle-gateway:latest"))

    def test_unreadable_inspect_returns_empty(self):
        with patch.object(util, "run_container_argv") as run:
            run.return_value = _completed("", returncode=1)
            self.assertEqual("", util.image_digest("x"))
            self.assertEqual("", util.container_image_digest("x"))
            self.assertEqual({}, util.container_env("x"))


class TestInspectContainerNetworkIp(unittest.TestCase):
    """inspect_container_network_ip must distinguish inspect failure (None)
    from 'no DHCP address yet' (""), which is the invariant live_source_ips
    relies on to skip reconciliation on partial snapshots."""

    _NETWORK = "bot-bottle-mac-gateway"

    def _inspect(self, stdout: str, returncode: int = 0) -> str | None:
        cp = util.subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )
        with patch.object(util.subprocess, "run", return_value=cp):
            return util.inspect_container_network_ip("bot-bottle-abc", self._NETWORK)

    def _entry(self, ip: str = "192.168.128.5") -> str:
        return (
            f'[{{"status":{{"networks":['
            f'{{"network":"{self._NETWORK}","ipv4Address":"{ip}"}}'
            f']}}}}]'
        )

    def test_returns_ip_when_inspect_succeeds(self) -> None:
        self.assertEqual("192.168.128.5", self._inspect(self._entry()))

    def test_strips_cidr_prefix(self) -> None:
        self.assertEqual("192.168.128.5", self._inspect(self._entry("192.168.128.5/24")))

    def test_returns_empty_string_when_no_address_assigned_yet(self) -> None:
        no_ip = f'[{{"status":{{"networks":[{{"network":"{self._NETWORK}","ipv4Address":""}}]}}}}]'
        self.assertEqual("", self._inspect(no_ip))

    def test_returns_empty_string_when_network_list_absent(self) -> None:
        self.assertEqual("", self._inspect('[{"status":{}}]'))

    def test_returns_none_on_nonzero_exit(self) -> None:
        self.assertIsNone(self._inspect("", returncode=1))

    def test_returns_none_on_malformed_json(self) -> None:
        self.assertIsNone(self._inspect("not-json"))

    def test_returns_none_on_unexpected_json_shape(self) -> None:
        self.assertIsNone(self._inspect("null"))


class TestWaitContainerIpv4(unittest.TestCase):
    def test_returns_address_once_dhcp_assigns_it(self):
        with patch.object(util, "try_container_ipv4_on_network", side_effect=["", "", "192.168.128.4"]), \
                patch.object(util.time, "sleep"):
            ip = util.wait_container_ipv4_on_network("c", "net", timeout=5, poll=0)
        self.assertEqual("192.168.128.4", ip)

    def test_returns_empty_on_timeout(self):
        with patch.object(util, "try_container_ipv4_on_network", return_value=""), \
                patch.object(util.time, "sleep"):
            self.assertEqual("", util.wait_container_ipv4_on_network("c", "net", timeout=-1))


class TestMacosContainerImageCreatedAt(unittest.TestCase):
    def _ok(self, stdout: str) -> "util.subprocess.CompletedProcess":  # type: ignore
        return util.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr="",
        )

    def _fail(self, stderr: str = "no such image") -> "util.subprocess.CompletedProcess":  # type: ignore
        return util.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=stderr,
        )

    def test_parses_iso_timestamp_from_dict(self):
        payload = '[{"created": "2025-06-01T12:00:00"}]'
        with patch.object(util.subprocess, "run", return_value=self._ok(payload)):
            dt = util.image_created_at("bot-bottle-agent:latest")
        self.assertEqual(2025, dt.year)
        self.assertEqual(6, dt.month)
        self.assertEqual(1, dt.day)

    def test_accepts_list_or_dict_input(self):
        # Container CLI may return a list; we take the first element.
        payload = '[{"created": "2024-01-15T08:30:00"}]'
        with patch.object(util.subprocess, "run", return_value=self._ok(payload)):
            dt = util.image_created_at("some-image:latest")
        self.assertEqual(2024, dt.year)

    def test_accepts_uppercase_Created_field(self):
        payload = '[{"Created": "2024-03-20T10:00:00"}]'
        with patch.object(util.subprocess, "run", return_value=self._ok(payload)):
            dt = util.image_created_at("some-image:latest")
        self.assertEqual(2024, dt.year)
        self.assertEqual(3, dt.month)

    def test_dies_on_nonzero_returncode(self):
        with patch.object(util.subprocess, "run", return_value=self._fail("not found")), \
             patch.object(util, "die", side_effect=SystemExit("die")) as die:
            with self.assertRaises(SystemExit):
                util.image_created_at("missing:tag")
        die.assert_called_once()
        self.assertIn("missing:tag", die.call_args.args[0])

    def test_dies_on_malformed_json(self):
        with patch.object(util.subprocess, "run", return_value=self._ok("not-json {")), \
             patch.object(util, "die", side_effect=SystemExit("die")) as die:
            with self.assertRaises(SystemExit):
                util.image_created_at("some:tag")
        die.assert_called_once()

    def test_returns_none_when_no_created_field(self):
        payload = '[{"id": "sha256:abc123"}]'
        with patch.object(util.subprocess, "run", return_value=self._ok(payload)):
            result = util.image_created_at("some:tag")
        self.assertIsNone(result)

    def test_returns_none_on_invalid_timestamp_format(self):
        payload = '[{"created": "not-a-date"}]'
        with patch.object(util.subprocess, "run", return_value=self._ok(payload)):
            result = util.image_created_at("some:tag")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
