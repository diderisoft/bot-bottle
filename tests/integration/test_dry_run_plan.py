"""Integration: cli.py start --dry-run renders the planned shape and
does not create any docker resources. Confirms the preflight contract
from PRD 0001 (allowlist line in the plan, no docker side effects)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._docker import skip_unless_docker

REPO_ROOT = Path(__file__).resolve().parent.parent


@skip_unless_docker()
class TestDryRunPlan(unittest.TestCase):
    def test_dry_run(self):
        work_dir = Path(tempfile.mkdtemp())
        try:
            manifest = work_dir / "claude-bottle.json"
            manifest.write_text(json.dumps({
                "bottles": {"dev": {"egress": {"allowlist": ["example.org"]}}},
                "agents": {
                    "demo": {"skills": [], "prompt": "", "bottle": "dev"},
                },
            }))

            nets_before = self._count_claude_bottle_networks()
            ctrs_before = self._count_claude_bottle_containers()

            env = os.environ.copy()
            env["HOME"] = str(work_dir)
            env["CLAUDE_BOTTLE_DRY_RUN"] = "1"
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "cli.py"), "start", "demo"],
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            out = result.stdout + result.stderr

            self.assertIn("egress", out, "preflight: egress line present")
            # 7 baked defaults + 1 bottle entry = 8.
            self.assertRegex(out, r"8 hosts allowed", "preflight: bottle entry counted")
            self.assertIn("api.anthropic.com", out, "preflight: baked default shown")
            self.assertRegex(out, r"runtime\s*:\s*runc", "preflight: default runtime shown")
            self.assertIn("dry-run requested", out, "dry-run banner present")
            self.assertNotIn("/dev/tty", out, "dry-run exited before tty prompt")

            self.assertEqual(nets_before, self._count_claude_bottle_networks(),
                             "no networks created")
            self.assertEqual(ctrs_before, self._count_claude_bottle_containers(),
                             "no containers created")
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

    def _count_claude_bottle_networks(self) -> int:
        result = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
        )
        return sum(1 for n in result.stdout.splitlines() if n.startswith("claude-bottle"))

    def _count_claude_bottle_containers(self) -> int:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        return sum(1 for n in result.stdout.splitlines() if n.startswith("claude-bottle"))


if __name__ == "__main__":
    unittest.main()
