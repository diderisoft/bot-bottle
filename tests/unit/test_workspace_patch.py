"""Unit: the agent's work, saved as a patch when the bottle goes."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot_bottle.backend import ExecResult
from bot_bottle.cli.launch_client import _report_work
from bot_bottle.control.hostctl.bottles import _save_work
from bot_bottle.workspace import patch_summary, save_patch, work_patch_path

_PATCH = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1,2 @@
 one
+two
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new
"""


class Patches(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_summary_counts_files_and_lines(self) -> None:
        self.assertEqual((2, 2, 0), patch_summary(_PATCH))

    def test_a_patch_is_private_and_an_empty_one_clears_the_old(self) -> None:
        path = save_patch("pi", _PATCH)
        assert path is not None
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertIsNone(save_patch("pi", "  \n"))
        self.assertFalse(path.exists())

    def bottle_plan(self, *, git: bool) -> tuple[MagicMock, object]:
        bottle = MagicMock()
        bottle.exec.return_value = ExecResult(returncode=0, stdout=_PATCH, stderr="")
        plan = SimpleNamespace(workspace_plan=SimpleNamespace(
            enabled=True, has_host_git_dir=git, guest_path="/home/node/workspace"))
        return bottle, plan

    def test_the_holder_saves_a_git_workspace_before_teardown(self) -> None:
        bottle, plan = self.bottle_plan(git=True)
        _save_work(bottle, plan.workspace_plan, "pi")  # type: ignore[arg-type]
        self.assertEqual(_PATCH, work_patch_path("pi").read_text())
        self.assertIn("git diff --cached --binary", bottle.exec.call_args.args[0])

    def test_nothing_is_saved_without_a_git_workspace_or_on_failure(self) -> None:
        bottle, plan = self.bottle_plan(git=False)
        _save_work(bottle, plan.workspace_plan, "pi")  # type: ignore[arg-type]
        bottle.exec.assert_not_called()
        bottle, plan = self.bottle_plan(git=True)
        bottle.exec.side_effect = OSError("bottle gone")
        with patch("sys.stderr", io.StringIO()):
            _save_work(bottle, plan.workspace_plan, "pi")  # type: ignore[arg-type]  # must not raise
        self.assertFalse(work_patch_path("pi").exists())

    def test_the_cli_reports_a_patch_from_this_session_only(self) -> None:
        save_patch("pi", _PATCH)
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            _report_work("pi", since=time.time() + 60)  # an older patch
        self.assertEqual("", err.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            _report_work("pi", since=time.time() - 60)
        self.assertIn("pi changed 2 files (+2 −0)", err.getvalue())
        self.assertIn("git apply", err.getvalue())
        self.assertIn(str(Path("work") / "pi.patch"), err.getvalue())

    def test_gitleaks_findings_warn_before_the_apply_line(self) -> None:
        from bot_bottle.workspace import scan_patch_for_secrets  # pylint: disable=import-outside-toplevel
        findings = ["github-token: token at key.py:3"]
        save_patch("pi", _PATCH)
        with patch("bot_bottle.cli.launch_client.scan_patch_for_secrets",
                   return_value=findings), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            _report_work("pi", since=time.time() - 60)
        output = err.getvalue()
        self.assertIn("gitleaks found 1 potential secret", output)
        self.assertIn("github-token: token at key.py:3", output)
        self.assertIn("git apply", output)  # still offered, with warning before it

    def test_no_gitleaks_binary_is_a_no_op(self) -> None:
        from bot_bottle.workspace import scan_patch_for_secrets  # pylint: disable=import-outside-toplevel
        import shutil  # pylint: disable=import-outside-toplevel
        with patch.object(shutil, "which", return_value=None):
            self.assertEqual([], scan_patch_for_secrets(_PATCH))

    def test_gitleaks_clean_patch_returns_empty(self) -> None:
        from bot_bottle.workspace import scan_patch_for_secrets  # pylint: disable=import-outside-toplevel
        import subprocess  # pylint: disable=import-outside-toplevel
        from bot_bottle.backend import ExecResult  # pylint: disable=import-outside-toplevel
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
        with patch("shutil.which", return_value="/usr/bin/gitleaks"), \
                patch("subprocess.run", return_value=result):
            self.assertEqual([], scan_patch_for_secrets(_PATCH))


if __name__ == "__main__":
    unittest.main()
