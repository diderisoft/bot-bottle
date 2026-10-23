"""Unit: the bot-bottle status bar in the agent's tmux session."""

from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main()
