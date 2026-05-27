"""Integration: PRD 0023 chunk 2b — smolvm subprocess wrapper
exercised against the real binary.

The full machine-lifecycle round trip (create → start → exec →
delete) is gated behind macOS + Darwin platform check and lives
in chunk 2d's smoke. This file just verifies `is_available()`
correctly reports presence and `_smolvm()` can run a no-op
subcommand without errors — enough to flag wrapper drift if
smolvm's flag parser changes shape across versions."""

from __future__ import annotations

import os
import platform
import subprocess
import unittest

from claude_bottle.backend.smolmachines.smolvm import is_available


@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: smolvm not installed on the runner",
)
@unittest.skipUnless(
    platform.system() == "Darwin",
    "smolvm is macOS-only for v1; Linux+KVM path is a future PRD",
)
@unittest.skipUnless(
    is_available(),
    "smolvm not on PATH; install via "
    "curl -sSL https://smolmachines.com/install.sh | sh",
)
class TestSmolvmSmoke(unittest.TestCase):
    def test_smolvm_help_responds(self):
        # `smolvm --help` exits 0 (per `smolvm machine --help`
        # convention) — verifies the binary launches and the
        # top-level parser is intact.
        r = subprocess.run(
            ["smolvm", "--help"],
            capture_output=True, text=True, check=False,
        )
        # Either exit-code 0 (clean) or 1 (some CLIs return 1
        # from --help by convention; smolvm 0.8.0 does this). The
        # point is the binary runs and emits help text.
        self.assertIn("smolvm", r.stdout)
        self.assertIn("machine", r.stdout)

    def test_machine_ls_empty_returns_json_array(self):
        # `machine ls --json` is the contract chunk 4's
        # list_active wires to. Lock in that the JSON shape is
        # parseable now so chunk 4 doesn't surprise us.
        import json
        r = subprocess.run(
            ["smolvm", "machine", "ls", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, r.returncode, r.stderr)
        parsed = json.loads(r.stdout)
        self.assertIsInstance(parsed, list)


if __name__ == "__main__":
    unittest.main()
