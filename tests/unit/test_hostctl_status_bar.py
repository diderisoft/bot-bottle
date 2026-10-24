"""Unit: the bot-bottle status bar in the agent's tmux session."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.control.hostctl import sessions


class StatusBar(unittest.TestCase):
    def setUp(self) -> None:
        # A short root: tmux and the control socket need short paths.
        self.root = Path(tempfile.mkdtemp(prefix="bbs.", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_the_config_turns_the_bar_on(self) -> None:
        text = sessions.write_config().read_text()
        self.assertIn("set -g status on", text)
        self.assertNotIn("status off", text)
        self.assertIn("bot-bottle", text)
        self.assertIn("Ctrl-] detach", text)

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_tmux_renders_it_with_the_agent(self) -> None:
        conf = sessions.write_config()
        server = ["tmux", "-L", f"bb-test-{os.getpid()}", "-f", str(conf)]
        self.addCleanup(subprocess.run, [*server, "kill-server"], capture_output=True, check=False)
        subprocess.run([*server, "new-session", "-d", "-s", "pi-2", "sleep 30"], check=True)
        subprocess.run([*server, "set-option", "-t", "=pi-2:", "@bb_agent", "pi"], check=True)
        left = subprocess.run([*server, "display-message", "-p", "-t", "pi-2:", "#{T:status-left}"],
                              capture_output=True, text=True, check=True).stdout
        self.assertIn("bot-bottle", left)
        self.assertIn("pi-2 · pi agent", left)

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_the_right_side_fits_the_client(self) -> None:
        conf = sessions.write_config()
        server = ["tmux", "-L", f"bb-test-r{os.getpid()}", "-f", str(conf)]
        self.addCleanup(subprocess.run, [*server, "kill-server"], capture_output=True, check=False)
        subprocess.run([*server, "new-session", "-d", "-s", "pi", "sleep 30"], check=True)
        right = subprocess.run([*server, "show-options", "-gv", "status-right"],
                               capture_output=True, text=True, check=True).stdout.strip()

        def render(width: int, **options: str) -> str:
            for name in ("blocks", "block_now", "block_host", "request"):
                subprocess.run([*server, "set-option", "-t", "=pi:", f"@bb_{name}",
                                options.get(name, "")], check=True)
            # No client attaches here, so stand the width in for client_width.
            fmt = right.replace("#{client_width}", str(width))
            out = subprocess.run([*server, "display-message", "-p", "-t", "=pi:", fmt],
                                 capture_output=True, text=True, check=True).stdout
            return re.sub(r"#\[[^]]*\]", "", out).strip()

        notice = {"blocks": "A12 S3", "block_now": "x.example · not on allowlist (+1)",
                  "block_host": "x.example (+1)"}
        self.assertEqual("Ctrl-] detach", render(120))
        self.assertEqual("⊘ A12 S3 Ctrl-] detach", render(160, blocks="A12 S3"))
        self.assertEqual("⊘ A12 S3", render(120, blocks="A12 S3"))
        self.assertEqual("⊘ x.example · not on allowlist (+1)  A12 S3 Ctrl-] detach",
                         render(160, **notice))
        self.assertEqual("⊘ x.example (+1)  A12 S3", render(120, **notice))
        request = "pi wants api.example.com and more besides"
        self.assertIn(request, render(120, blocks="A1", request=request))
        self.assertIn(request[:30] + " · Ctrl-", render(80, blocks="A1", request=request))


if __name__ == "__main__":
    unittest.main()
