"""Unit tests for Firecracker active-agent enumeration."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend import EnumerationError
from bot_bottle.backend.firecracker import enumerate as fc_enumerate
from bot_bottle.bottle_state import BottleMetadata


class TestEnumerateActive(unittest.TestCase):
    def test_maps_live_run_dirs_to_active_agents(self) -> None:
        metadata = BottleMetadata(
            identity="dev-a",
            agent_name="claude",
            cwd="",
            copy_cwd=False,
            started_at="2026-07-26T12:00:00Z",
            label="review",
            color="blue",
        )
        with (
            patch.object(
                fc_enumerate, "live_run_dirs",
                return_value=(Path("/cache/run/dev-a"),),
            ),
            patch.object(fc_enumerate, "read_metadata", return_value=metadata),
        ):
            agents = fc_enumerate.enumerate_active()
        self.assertEqual(1, len(agents))
        self.assertEqual("firecracker", agents[0].backend_name)
        self.assertEqual("dev-a", agents[0].slug)
        self.assertEqual("claude", agents[0].agent_name)
        self.assertEqual("review", agents[0].label)
        self.assertEqual((), agents[0].services)

    def test_missing_metadata_uses_safe_defaults(self) -> None:
        with (
            patch.object(
                fc_enumerate, "live_run_dirs",
                return_value=(Path("/cache/run/dev-a"),),
            ),
            patch.object(fc_enumerate, "read_metadata", return_value=None),
        ):
            agent = fc_enumerate.enumerate_active()[0]
        self.assertEqual("?", agent.agent_name)
        self.assertEqual("", agent.started_at)

    def test_process_scan_failure_propagates(self) -> None:
        with (
            patch.object(
                fc_enumerate, "live_run_dirs",
                side_effect=EnumerationError("pgrep failed"),
            ),
            self.assertRaisesRegex(EnumerationError, "pgrep failed"),
        ):
            fc_enumerate.enumerate_active()


if __name__ == "__main__":
    unittest.main()
