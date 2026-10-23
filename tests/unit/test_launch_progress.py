"""Unit: one-line-per-stage launch progress (PRD 0094 chunk 1)."""

from __future__ import annotations

import io
import re
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli.progress import LaunchProgress
from bot_bottle.control.hostctl.jobs import JobRegistry
from bot_bottle.lib import log


class _Out(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class Progress(unittest.TestCase):
    """Plain lines: no terminal, or a terminal with the screen turned off."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.log_path = Path(tmp.name) / "logs" / "launch.log"

    def progress(self, *, tty: bool = False, verbose: bool = False) -> tuple[LaunchProgress, _Out]:
        out = _Out(tty)
        return LaunchProgress(self.log_path, verbose=verbose, out=out, screen=False,
                              color=tty), out

    def run_launch(self, progress: LaunchProgress) -> None:
        progress.stage("prepare", "Reading the bottle")
        progress.line("bot-bottle: resolved agent pi")
        progress.stage("image", "Getting images")
        progress.line("bot-bottle: warning: image is 9 days old")
        progress.line("bot-bottle: pulled bot-bottle-pi")

    def test_quiet_shows_stages_and_warnings_only(self) -> None:
        progress, out = self.progress()
        self.run_launch(progress)
        progress.done("pi is running")
        text = out.getvalue()
        for expected in ("✓ Reading the bottle", "✓ Getting images", "✓ pi is running",
                         "warning: image is 9 days old"):
            self.assertIn(expected, text)
        self.assertNotIn("resolved agent", text)

    def test_a_repeated_stage_retitles_its_line(self) -> None:
        progress, out = self.progress()
        progress.stage("services", "Starting bot-bottle services")
        progress.stage("services", "Rebuilding bot-bottle services")
        progress.stage("start", "Starting the bottle")
        progress.done()
        self.assertEqual(1, out.getvalue().count("Rebuilding bot-bottle services"))
        self.assertNotIn("Starting bot-bottle services", out.getvalue())

    def test_success_ends_with_the_log_path(self) -> None:
        progress, out = self.progress()
        self.run_launch(progress)
        progress.done("pi is running")
        self.assertTrue(out.getvalue().rstrip().endswith("launch.log"))

    def test_every_line_goes_to_the_log_file(self) -> None:
        progress, _ = self.progress()
        self.run_launch(progress)
        progress.done()
        logged = self.log_path.read_text()
        for expected in ("== Reading the bottle", "resolved agent pi", "pulled bot-bottle-pi"):
            self.assertIn(expected, logged)

    def test_verbose_streams_everything(self) -> None:
        progress, out = self.progress(verbose=True)
        self.run_launch(progress)
        progress.done()
        self.assertIn("resolved agent pi", out.getvalue())

    def test_failure_shows_the_stage_the_tail_and_the_log_path(self) -> None:
        progress, out = self.progress()
        self.run_launch(progress)
        progress.fail("bot-bottle: image pull failed")
        text = out.getvalue()
        for expected in ("✗ Getting images", "pulled bot-bottle-pi", str(self.log_path)):
            self.assertIn(expected, text)

    def test_since_stage_is_the_current_stage_only(self) -> None:
        progress, _ = self.progress()
        self.run_launch(progress)
        self.assertEqual(["bot-bottle: warning: image is 9 days old",
                          "bot-bottle: pulled bot-bottle-pi"], progress.since_stage())
        progress.close()

    def test_finished_stages_get_a_green_check_on_a_terminal(self) -> None:
        progress, out = self.progress(tty=True)
        self.run_launch(progress)
        progress.done()
        self.assertIn("\x1b[32m✓\x1b[0m Reading the bottle", out.getvalue())


class Screen(unittest.TestCase):
    """The alternate screen: frames while launching, a summary after."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.log_path = Path(tmp.name) / "launch.log"
        self.size = patch("bot_bottle.cli.progress._size", return_value=(100, 30))
        self.size.start()
        self.addCleanup(self.size.stop)

    def progress(self, *, color: bool = True) -> tuple[LaunchProgress, _Out]:
        out = _Out(True)
        return LaunchProgress(self.log_path, out=out, tty=True, screen=True, color=color), out

    def test_the_launch_draws_on_the_alternate_screen_and_leaves_a_summary(self) -> None:
        progress, out = self.progress()
        progress.stage("prepare", "Reading the bottle")
        progress.stage("image", "Getting images")
        progress.line("bot-bottle: warning: image is 9 days old")
        progress.done("pi is running")
        text = out.getvalue()
        self.assertTrue(text.startswith("\x1b[?1049h"))
        during, after = text.split("\x1b[?1049l", 1)
        self.assertIn("\x1b[?2026h", during)  # repaints are synchronized
        self.assertIn("▀", during)  # the bottle
        plain_after = re.sub(r"\x1b\[[0-9;]*m", "", after)
        for expected in ("✓ Reading the bottle", "✓ Getting images",
                         "warning: image is 9 days old", "✓ pi is running", "launch log:"):
            self.assertIn(expected, plain_after)

    def test_only_changed_lines_are_rewritten(self) -> None:
        progress, out = self.progress()
        progress.stage("image", "Getting images")
        before = len(out.getvalue())
        progress.line("bot-bottle: just a log line")  # not shown: nothing changes
        self.assertEqual(before, len(out.getvalue()))
        progress.close()

    def test_a_narrow_terminal_drops_the_bottle(self) -> None:
        progress, out = self.progress()
        progress.stage("image", "Getting images")
        with patch("bot_bottle.cli.progress._size", return_value=(50, 30)):
            progress._on_resize(0, None)  # pylint: disable=protected-access
            progress.stage("start", "Starting the bottle")
        last = out.getvalue().rsplit("\x1b[2J", 1)[-1]  # the resize cleared the screen
        self.assertIn("Starting the bottle", last)
        self.assertNotIn("▀", last)
        progress.close()

    def test_a_question_gets_the_normal_screen(self) -> None:
        progress, out = self.progress()
        progress.stage("prepare", "Reading the bottle")
        progress.pause()
        self.assertTrue(out.getvalue().endswith("\x1b[?25h\x1b[?1049l"))
        before = len(out.getvalue())
        progress.line("bot-bottle: warning: shown as plain text")
        self.assertIn("shown as plain text", out.getvalue()[before:])
        self.assertNotIn("\x1b[?1049h", out.getvalue()[before:])
        progress.close()

    def test_failure_summarises_after_leaving_the_screen(self) -> None:
        progress, out = self.progress()
        progress.stage("image", "Getting images")
        progress.line("bot-bottle: pulled half")
        progress.fail("bot-bottle: pull failed")
        after = re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue().split("\x1b[?1049l", 1)[1])
        for expected in ("✗ Getting images", "pulled half", "pull failed", "full log:"):
            self.assertIn(expected, after)

    def test_the_bottle_cursor_blinks(self) -> None:
        from bot_bottle.cli import logo  # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.progress import logo_rows  # pylint: disable=import-outside-toplevel

        for truecolor in (True, False):
            on = logo_rows(truecolor=truecolor)
            off = logo_rows(truecolor=truecolor, cursor=False)
            self.assertEqual([logo.CURSOR[0] // 2],
                             [i for i, (a, b) in enumerate(zip(on, off)) if a != b])
        progress, _ = self.progress()
        # The screen picks 24-bit or 256 colors from COLORTERM; use its own rows.
        on, off = progress._logo, progress._logo_blink  # pylint: disable=protected-access
        progress.stage("image", "Getting images")
        lit = progress._layout()  # pylint: disable=protected-access
        progress._frame = 5  # pylint: disable=protected-access  # half a second later
        dark = progress._layout()  # pylint: disable=protected-access
        progress.close()
        self.assertTrue(any(on[logo.CURSOR[0] // 2] in line for line in lit))
        self.assertTrue(any(off[logo.CURSOR[0] // 2] in line for line in dark))

    def test_the_block_is_centered(self) -> None:
        from bot_bottle.cli import logo  # pylint: disable=import-outside-toplevel

        progress, _ = self.progress()
        progress.stage("image", "Getting images")
        lines = progress._layout()  # pylint: disable=protected-access
        progress.close()
        block = 40 + 4 + len(logo.ART[0])  # stage column, gap, bottle
        stage = next(line for line in lines if "Getting images" in line)
        self.assertEqual((100 - block) // 2, len(stage) - len(stage.lstrip()))
        top = next(i for i, line in enumerate(lines) if line.strip())
        self.assertEqual((30 - (len(logo.ART) + 1) // 2) // 2, top)  # vertically too

    def test_no_color_means_no_bottle(self) -> None:
        progress, out = self.progress(color=False)
        progress.stage("image", "Getting images")
        progress.close()
        self.assertNotIn("▀", out.getvalue())

    def test_verbose_never_uses_the_screen(self) -> None:
        progress = LaunchProgress(self.log_path, out=_Out(True), tty=True, verbose=True)
        self.assertFalse(progress.screen)
        progress.close()


class Pruning(unittest.TestCase):
    def test_keeps_the_newest_launch_logs_and_nothing_else_is_touched(self) -> None:
        import os  # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.progress import prune_launch_logs  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            for i in range(5):
                path = logs / f"launch-pi-{i}.log"
                path.write_text("x")
                os.utime(path, (1000 + i, 1000 + i))
            (logs / "supervise-crash.log").write_text("keep me")
            prune_launch_logs(logs, keep=2)
            self.assertEqual(["launch-pi-3.log", "launch-pi-4.log", "supervise-crash.log"],
                             sorted(p.name for p in logs.iterdir()))

    def test_a_new_launch_log_makes_room_for_itself(self) -> None:
        from bot_bottle.cli import progress as progress_mod  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict("os.environ", {"BOT_BOTTLE_ROOT": tmp}), \
                patch.object(progress_mod, "KEEP_LAUNCH_LOGS", 3):
            logs = Path(tmp) / "logs"
            logs.mkdir()
            for i in range(5):
                (logs / f"launch-old-{i}.log").write_text("x")
            path = progress_mod.launch_log_path("pi")
            path.write_text("new")
            self.assertEqual(3, len(list(logs.glob("launch-*.log"))))


class StageEvents(unittest.TestCase):
    def test_a_job_records_stage_markers_as_stage_events(self) -> None:
        def work() -> dict[str, object]:
            log.stage("image", "Getting images")
            log.info("pulled")
            return {}

        job = JobRegistry().start("t", "cli", work)
        events = [{k: v for k, v in e.items() if k != "seq"} for e in job.follow(timeout=5)]
        self.assertIn({"type": "stage", "stage": "image", "title": "Getting images"}, events)
        self.assertFalse(any("\x1f" in str(e.get("line", "")) for e in events))

    def test_without_a_sink_a_stage_logs_like_info(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            log.stage("image", "Getting images")
        self.assertEqual("bot-bottle: Getting images\n", err.getvalue())

    def test_follow_hands_stages_to_the_callback(self) -> None:
        from bot_bottle.control.hostctl.client import HostControlClient  # pylint: disable=import-outside-toplevel

        client = HostControlClient.__new__(HostControlClient)
        events = [{"type": "stage", "stage": "image", "title": "Getting images"},
                  {"type": "log", "line": "x"}, {"type": "result", "value": {}}]
        stages: list[tuple[str, str]] = []
        lines: list[str] = []
        with patch.object(HostControlClient, "events", return_value=iter(events)):
            final = client.follow("j", lines.append, lambda n, t: stages.append((n, t)))
        self.assertEqual(([("image", "Getting images")], ["x"]), (stages, lines))
        self.assertEqual("result", (final or {}).get("type"))


class ServiceStages(unittest.TestCase):
    """Bringing up the services is its own stage within a launch."""

    def stages(self, bring_up: "Callable[..., str]") -> list[str]:
        from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
        from bot_bottle.backend.infra_service import InfraService  # pylint: disable=import-outside-toplevel

        lines: list[str] = []
        with log.redirect(lines.append):
            try:
                fake = SimpleNamespace(_ensure_running=bring_up)
                InfraService.ensure_running(fake, launch_stages=True)  # type: ignore[arg-type]
            except RuntimeError:
                pass
        return [m[0] for m in map(log.parse_stage, lines) if m]

    def test_services_then_the_bottle(self) -> None:
        def up(**_: object) -> str:
            return "http://o"
        self.assertEqual(["services", "start"], self.stages(up))

    def test_the_bottle_stage_follows_even_a_failed_bring_up(self) -> None:
        def boom(**_: object) -> str:
            raise RuntimeError("down")
        self.assertEqual(["services", "start"], self.stages(boom))


if __name__ == "__main__":
    unittest.main()
