"""Unit: one-line-per-stage launch progress (PRD 0094 chunk 1)."""

from __future__ import annotations

import io
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
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.log_path = Path(tmp.name) / "logs" / "launch.log"

    def progress(self, *, tty: bool = False, verbose: bool = False) -> tuple[LaunchProgress, _Out]:
        out = _Out(tty)
        return LaunchProgress(self.log_path, verbose=verbose, out=out), out

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
        self.assertIn("✓ Reading the bottle", text)
        self.assertIn("✓ Getting images", text)
        self.assertIn("✓ pi is running", text)
        self.assertIn("warning: image is 9 days old", text)
        self.assertNotIn("resolved agent", text)

    def test_a_repeated_stage_retitles_its_line(self) -> None:
        progress, out = self.progress()
        progress.stage("services", "Starting bot-bottle services")
        progress.stage("services", "Rebuilding bot-bottle services")
        progress.stage("services", "Rebuilding bot-bottle services")
        progress.stage("start", "Starting the bottle")
        progress.done()
        text = out.getvalue()
        self.assertEqual(1, text.count("Rebuilding bot-bottle services"))
        self.assertNotIn("Starting bot-bottle services", text)

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
        self.assertIn("✗ Getting images", text)
        self.assertIn("pulled bot-bottle-pi", text)
        self.assertIn(str(self.log_path), text)

    def test_since_stage_is_the_current_stage_only(self) -> None:
        progress, _ = self.progress()
        self.run_launch(progress)
        self.assertEqual(["bot-bottle: warning: image is 9 days old",
                          "bot-bottle: pulled bot-bottle-pi"], progress.since_stage())
        progress.close()

    def test_nothing_redraws_over_a_question(self) -> None:
        progress, out = self.progress(tty=True)
        progress.stage("prepare", "Reading the bottle")
        progress.pause()
        before = out.getvalue()
        progress._draw("x")  # pylint: disable=protected-access  # what the spinner calls
        progress.line("bot-bottle: warning: shown as plain text")
        self.assertNotIn("Reading the bottle", out.getvalue()[len(before):])
        progress.close()

    def test_a_terminal_redraws_in_place(self) -> None:
        progress, out = self.progress(tty=True)
        self.run_launch(progress)
        progress.done()
        self.assertIn("\r\x1b[K", out.getvalue())


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
