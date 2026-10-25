"""Unit: the bot-bottle status bar in the agent's tmux session."""

from __future__ import annotations

import os
import pty
import re
import select
import signal
import shutil
import subprocess
import tempfile
import time
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
            for name in ("blocks", "block_now", "block_host", "request", "warning"):
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
        warning = "isolation gap: agent can reach the host at 10.0.0.1 (+1 more)"
        self.assertEqual(f"⚠ {warning}  Ctrl-] detach", render(120, warning=warning))
        self.assertEqual(f"⊘ A1 ⚠ {warning[:30]}…", render(80, blocks="A1", warning=warning))
        self.assertNotIn("⚠", render(120, warning=warning, request=request))

    def test_the_status_warning_is_short_and_safe(self) -> None:
        long = ("isolation gap: agent can reach the host at 10.0.0.1; public addresses "
                "and DNS are blocked. See #678.")
        self.assertEqual("isolation gap: agent can reach the host at 10.0.0.1",
                         sessions.status_warning([long]))
        self.assertEqual("image is 9 days old (+1 more)",
                         sessions.status_warning(["image is 9 days old. Rebuild it.", long]))
        self.assertEqual("a ##[fg=red] b", sessions.status_warning(["a #[fg=red] b"]))
        self.assertEqual("", sessions.status_warning([]))

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_tmux_shows_a_launch_warning_before_the_detach_key(self) -> None:
        conf = sessions.write_config()
        server = ["tmux", "-L", f"bb-test-w-{os.getpid()}", "-f", str(conf)]
        self.addCleanup(subprocess.run, [*server, "kill-server"], capture_output=True, check=False)
        subprocess.run([*server, "new-session", "-d", "-s", "pi-3", "sleep 30"], check=True)

        def right() -> str:
            fmt = subprocess.run([*server, "show-options", "-gv", "status-right"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            # No client attaches here, so stand a 120-column one in.
            return subprocess.run([*server, "display-message", "-p", "-t", "pi-3:",
                                   fmt.replace("#{client_width}", "120")],
                                  capture_output=True, text=True, check=True).stdout

        self.assertNotIn("⚠", right())
        warning = sessions.status_warning(["isolation gap: agent can reach the host #1; more"])
        subprocess.run([*server, "set-option", "-t", "=pi-3:", "@bb_warning", warning], check=True)
        shown = right()
        # Expanded, not yet drawn: `##` is what the drawing turns into `#`, so a
        # warning can't smuggle in a style.
        self.assertIn("#[fg=colour178]⚠ isolation gap: agent can reach the host ##1", shown)
        self.assertIn("Ctrl-] detach", shown)

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_a_viewer_never_gets_the_agents_clipboard_or_passthrough_escapes(self) -> None:
        conf = sessions.write_config()
        tm = ["tmux", "-L", f"bb-test-esc-{os.getpid()}", "-f", str(conf)]
        self.addCleanup(subprocess.run, [*tm, "kill-server"], capture_output=True, check=False)
        hostile = (r"sleep 1; printf '\033]52;c;RVZJTA==\a'; "
                   r"printf '\033Ptmux;\033\033]52;c;UEFTUw==\a\033\\'; "
                   r"printf '\033]8;;https://evil.example/\alink\033]8;;\a'; "
                   r"printf ' agent-done'; sleep 30")
        subprocess.run([*tm, "new-session", "-d", "-s", "esc", "-x", "80", "-y", "10",
                        "sh", "-c", hostile], check=True)
        pid, fd = pty.fork()
        if pid == 0:  # a viewer, attached the way the console terminal attaches
            os.environ["TERM"] = "xterm-256color"
            os.execvp("tmux", [*tm, "attach", "-r", "-t", "esc"])
        self.addCleanup(os.kill, pid, signal.SIGKILL)
        seen, deadline = b"", time.monotonic() + 8
        while b"agent-done" not in seen and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    break
        self.assertIn(b"agent-done", seen)
        for needle in (b"\x1b]52", b"RVZJTA", b"UEFTUw", b"\x1bPtmux", b"evil.example"):
            self.assertNotIn(needle, seen)
        show = subprocess.run([*tm, "show-options", "-g", "set-clipboard"], capture_output=True,
                              text=True, check=True).stdout
        self.assertEqual("set-clipboard off", show.strip())


if __name__ == "__main__":
    unittest.main()
