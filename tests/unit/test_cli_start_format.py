"""Unit: argparse-level CLI checks for `start --format`.

Lives in tests/unit/ because nothing here touches Docker — the CLI
exits at argument-validation time before any backend code runs.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestStartFormatFlag(unittest.TestCase):
    def test_json_format_requires_dry_run(self):
        """Emitting JSON in a real run would race the y/N prompt; the
        CLI must reject the combination with a message that names the
        offending flag."""
        work_dir = Path(tempfile.mkdtemp())
        try:
            (work_dir / "claude-bottle.json").write_text(json.dumps({
                "bottles": {"dev": {}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            }))
            env = os.environ.copy()
            env["HOME"] = str(work_dir)
            env.pop("CLAUDE_BOTTLE_DRY_RUN", None)
            result = subprocess.run(
                [
                    sys.executable, str(REPO_ROOT / "cli.py"),
                    "start", "--format", "json", "demo",
                ],
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "--format=json requires --dry-run", result.stderr,
                f"expected the flag-conflict message; got stderr={result.stderr!r}",
            )
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
