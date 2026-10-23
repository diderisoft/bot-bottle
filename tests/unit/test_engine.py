"""Unit: `engine_id()` names the running bot-bottle code."""

from __future__ import annotations

import json
import subprocess
import unittest
from importlib import metadata
from unittest import mock

from bot_bottle import engine


def _dist(version: str, direct_url: str | None) -> mock.Mock:
    dist = mock.Mock(spec=metadata.Distribution)
    dist.version = version
    dist.read_text.return_value = direct_url
    return dist


class EngineId(unittest.TestCase):
    def setUp(self) -> None:
        engine.engine_id.cache_clear()
        self.addCleanup(engine.engine_id.cache_clear)

    def test_a_checkout_reports_its_version_and_head(self) -> None:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        with mock.patch.object(engine.resources, "is_source_checkout", return_value=True):
            got = engine.engine_id()
        self.assertRegex(got, rf"^bot-bottle/\d[^+]*\+{sha[:12]}$")

    def test_a_git_install_reports_the_commit_pip_recorded(self) -> None:
        url = json.dumps({"url": "https://example/repo.git",
                          "vcs_info": {"vcs": "git", "commit_id": "abcdef0123456789"}})
        with mock.patch.object(engine.resources, "is_source_checkout", return_value=False), \
             mock.patch.object(engine.metadata, "distribution",
                               return_value=_dist("0.1.4", url)):
            self.assertEqual(engine.engine_id(), "bot-bottle/0.1.4+abcdef012345")

    def test_a_wheel_install_reports_the_version_alone(self) -> None:
        with mock.patch.object(engine.resources, "is_source_checkout", return_value=False), \
             mock.patch.object(engine.metadata, "distribution",
                               return_value=_dist("0.1.4", None)):
            self.assertEqual(engine.engine_id(), "bot-bottle/0.1.4")

    def test_a_checkout_without_git_reports_the_version_alone(self) -> None:
        with mock.patch.object(engine.resources, "is_source_checkout", return_value=True), \
             mock.patch.object(engine.subprocess, "run", side_effect=OSError):
            self.assertRegex(engine.engine_id(), r"^bot-bottle/\d[^+]*$")


if __name__ == "__main__":
    unittest.main()
