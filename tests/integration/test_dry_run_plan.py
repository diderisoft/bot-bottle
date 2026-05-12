"""Integration: cli.py start --dry-run --format=json renders a stable
machine-readable plan and creates zero Docker resources. The shape of
the JSON document is part of the CLI's user-facing contract."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._docker import skip_unless_docker

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@skip_unless_docker()
class TestDryRunPlan(unittest.TestCase):
    def test_dry_run_emits_structured_plan(self):
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
            env.pop("CLAUDE_BOTTLE_DRY_RUN", None)
            result = subprocess.run(
                [
                    sys.executable, str(REPO_ROOT / "cli.py"),
                    "start", "--dry-run", "--format", "json", "demo",
                ],
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                0, result.returncode,
                f"start --dry-run failed: stderr={result.stderr}",
            )

            plan = json.loads(result.stdout)

            self.assertEqual("demo", plan["agent"])
            self.assertEqual("dev", plan["bottle"])
            self.assertEqual("runc", plan["runtime"],
                             "runsc isn't available on the CI runner")
            self.assertEqual([], plan["skills"])
            self.assertEqual([], plan["ssh_hosts"])
            self.assertEqual(False, plan["remote_control"])
            self.assertEqual(0, plan["prompt"]["length"])

            # User-declared host + a baked default both present; the union
            # is sorted and deduplicated.
            hosts = plan["egress"]["hosts"]
            self.assertIn("example.org", hosts)
            self.assertIn("api.anthropic.com", hosts)
            self.assertEqual(plan["egress"]["host_count"], len(hosts))
            self.assertEqual(sorted(set(hosts)), hosts,
                             "hosts must be sorted and deduplicated")

            # No Docker side effects.
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
            check=True,
        )
        return sum(1 for n in result.stdout.splitlines() if n.startswith("claude-bottle"))

    def _count_claude_bottle_containers(self) -> int:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return sum(1 for n in result.stdout.splitlines() if n.startswith("claude-bottle"))


if __name__ == "__main__":
    unittest.main()
