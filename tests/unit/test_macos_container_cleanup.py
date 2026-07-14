"""Unit: Apple Container cleanup/enumeration helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

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
        with patch.object(cleanup.subprocess, "run") as run:
            cleanup.cleanup(plan)
        self.assertEqual(
            ["container", "delete", "--force", "bot-bottle-a"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual(
            ["container", "network", "delete", "bot-bottle-net-a"],
            run.call_args_list[1].args[0],
        )


class TestMacosContainerEnumerate(unittest.TestCase):
    def test_enumerate_active_is_empty_while_disabled(self):
        # The macOS backend is disabled during the companion-container removal cleanup
        # (#385); it launches nothing, so there is nothing to enumerate.
        self.assertEqual([], enum_mod.enumerate_active())


if __name__ == "__main__":
    unittest.main()
