"""Unit: the `log: 2` full egress log is a capped, rotated, owner-only file
(issue #934), never stderr."""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import types
import unittest
from typing import Any
from unittest.mock import patch

from bot_bottle.gateway.egress import full_log
from bot_bottle.gateway.egress.full_log import FullLog


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class _Headers(dict[str, str]):
    pass


def _flow(body: str = "hi") -> Any:
    request = types.SimpleNamespace(
        host="api.example.com", method="POST", path="/v1",
        headers=_Headers({"Authorization": "Bearer x", "X-A": "1"}),
        get_text=lambda strict=False: body)
    response = types.SimpleNamespace(
        status_code=200, headers=_Headers({"Content-Type": "text/plain"}),
        get_text=lambda strict=False: body)
    return types.SimpleNamespace(request=request, response=response)


class TestFullLog(unittest.TestCase):
    def test_entries_go_to_an_owner_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = FullLog(d)
            log.request(_flow(), {})
            log.response(_flow(), {})
            self.assertEqual(_mode(log.path), 0o600)
            entries = [json.loads(x) for x in open(log.path, encoding="utf-8")]
        self.assertEqual([e["event"] for e in entries], ["egress_request", "egress_response"])
        self.assertNotIn("Authorization", entries[0]["headers"])
        self.assertEqual(entries[0]["body"], "hi")

    def test_a_large_body_is_cut_with_its_full_length(self) -> None:
        big = "x" * (full_log.BODY_LIMIT_BYTES + 10)
        with tempfile.TemporaryDirectory() as d:
            log = FullLog(d)
            log.request(_flow(big), {})
            entry = json.loads(open(log.path, encoding="utf-8").read())
        self.assertEqual(len(entry["body"]), full_log.BODY_LIMIT_BYTES)
        self.assertTrue(entry["body_truncated"])
        self.assertEqual(entry["body_bytes"], len(big))

    def test_rotates_and_keeps_at_most_the_backups(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = FullLog(d, max_bytes=200, backups=2)
            for i in range(30):
                log.write({"n": i, "pad": "y" * 40})
            names = sorted(os.listdir(d))
            sizes = [os.path.getsize(os.path.join(d, n)) for n in names]
            newest = open(log.path, encoding="utf-8").read().splitlines()[-1]
        self.assertEqual(names, ["full.jsonl", "full.jsonl.1", "full.jsonl.2"])
        self.assertTrue(all(s <= 200 for s in sizes), sizes)
        self.assertEqual(json.loads(newest)["n"], 29)

    def test_no_backups_starts_the_file_over(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = FullLog(d, max_bytes=100, backups=0)
            for i in range(10):
                log.write({"n": i, "pad": "z" * 30})
            self.assertEqual(os.listdir(d), ["full.jsonl"])
            self.assertLessEqual(os.path.getsize(log.path), 100)

    def test_a_write_failure_never_raises(self) -> None:
        log = FullLog("/nonexistent/dir/for/sure")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            log.write({"n": 1})
        self.assertIn("full log write failed", buf.getvalue())

    def test_from_env_uses_the_configured_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "logs")
            log = FullLog.from_env({"EGRESS_FULL_LOG_DIR": target})
            self.assertEqual(log.path, os.path.join(target, full_log.FILE_NAME))
            self.assertEqual(_mode(target), 0o700)

    def test_from_env_falls_back_to_a_private_temp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "file")
            open(blocker, "w", encoding="utf-8").close()
            buf = io.StringIO()
            with patch("sys.stderr", buf), patch("tempfile.gettempdir", return_value=d):
                env: Any = {"EGRESS_FULL_LOG_DIR": os.path.join(blocker, "x")}
                log = FullLog.from_env(env)
            self.assertEqual(log.path, os.path.join(d, "bot-bottle-egress", full_log.FILE_NAME))
        self.assertIn("unusable", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
