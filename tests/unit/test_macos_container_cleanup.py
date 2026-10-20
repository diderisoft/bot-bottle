"""Unit: Apple Container cleanup/enumeration helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend import EnumerationError
from bot_bottle.backend.cleanup_control import CleanupError
from bot_bottle.backend.macos_container import cleanup, enumerate as enum_mod
from bot_bottle.backend.macos_container.bottle_cleanup_plan import (
    MacosContainerBottleCleanupPlan,
)


class TestMacosContainerCleanup(unittest.TestCase):
    def test_lists_prefixed_containers(self):
        completed = cleanup.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="bot-bottle-a\nbot-bottle-b\nother\n",
            stderr="",
        )
        with patch.object(cleanup.subprocess, "run", return_value=completed):
            self.assertEqual(
                ["bot-bottle-a", "bot-bottle-b"],
                cleanup._list_prefixed_containers(),
            )

    def test_cleanup_deletes_containers_and_networks(self):
        plan = MacosContainerBottleCleanupPlan(
            containers=("bot-bottle-a",),
            networks=("bot-bottle-net-a",),
        )
        completed = cleanup.subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch.object(cleanup.subprocess, "run", return_value=completed) as run:
            cleanup.cleanup(plan, Settings())
        self.assertEqual(
            ["container", "delete", "--force", "bot-bottle-a"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            ["container", "network", "delete", "bot-bottle-net-a"],
            run.call_args_list[1].args[0],
        )

    def test_cleanup_attempts_all_resources_then_raises(self):
        plan = MacosContainerBottleCleanupPlan(
            containers=("bot-bottle-a",), networks=("bot-bottle-net-a",),
        )
        failed = cleanup.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=failed) as run, \
             self.assertRaisesRegex(CleanupError, "unavailable"):
            cleanup.cleanup(plan, Settings())
        self.assertEqual(2, run.call_count)

    def test_container_enumeration_failure_aborts(self):
        completed = cleanup.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="service unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=completed), \
                self.assertRaisesRegex(EnumerationError, "service unavailable"):
            cleanup._list_prefixed_containers()

    def test_network_enumeration_failure_aborts(self):
        completed = cleanup.subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="service unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=completed), \
                self.assertRaisesRegex(EnumerationError, "service unavailable"):
            cleanup._list_prefixed_networks()


class TestMacosContainerEnumerate(unittest.TestCase):
    """The backend launches bottles again (PRD 0070), so enumeration is real
    rather than the disabled-era stub. These must not shell out: `container`
    does not exist on the Linux CI host."""

    def _enumerate(self, stdout: str, returncode: int = 0):
        completed = enum_mod.subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="",
        )
        with patch.object(enum_mod.subprocess, "run", return_value=completed), \
                patch.object(enum_mod, "read_metadata", return_value=None):
            return enum_mod.enumerate_active()

    def test_lists_agent_containers_by_slug(self):
        agents = self._enumerate("bot-bottle-dev-abc\nunrelated\n")
        self.assertEqual(["dev-abc"], [a.slug for a in agents])
        self.assertEqual(["macos-container"], [a.backend_name for a in agents])

    def test_excludes_both_infra_containers(self):
        """Both infra containers (orchestrator + gateway) share the bot-bottle-
        prefix but are infrastructure — listing either would invent a phantom
        agent per host."""
        agents = self._enumerate(
            "bot-bottle-mac-infra\nbot-bottle-mac-orchestrator\nbot-bottle-dev-abc\n")
        self.assertEqual(["dev-abc"], [a.slug for a in agents])

    def test_raises_when_the_cli_fails(self):
        with patch.object(enum_mod.container_mod, "service_running", return_value=True), \
             self.assertRaises(EnumerationError):
            self._enumerate("", returncode=1)

    def test_raises_typed_error_when_cli_is_missing(self):
        with (
            patch.object(
                enum_mod.subprocess, "run", side_effect=FileNotFoundError,
            ),
            self.assertRaisesRegex(EnumerationError, "CLI not found"),
        ):
            enum_mod.enumerate_active()


if __name__ == "__main__":
    unittest.main()
