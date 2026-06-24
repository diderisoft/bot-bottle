"""Unit: leveled + structured logging wrappers (issue #252).

Locks three properties of bot_bottle.log:
  - backward compatibility — default output is byte-identical to the
    original bare wrappers, so the 100+ existing single-string call
    sites are unaffected;
  - context rendering — an optional mapping becomes a parseable
    ` [k=v ...]` suffix;
  - level gating — BOT_BOTTLE_LOG_LEVEL filters by severity, debug is
    silent by default, and error always surfaces.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from typing import Callable
from unittest import mock

from bot_bottle import log


def _capture(
    fn: Callable[..., None],
    *args: object,
    env: dict[str, str] | None = None,
    **kwargs: object,
) -> str:
    buf = io.StringIO()
    patched = mock.patch.dict("os.environ", env or {}, clear=False)
    with patched, contextlib.redirect_stderr(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class TestBackwardCompat(unittest.TestCase):
    """No context + default level → exactly the legacy lines."""

    def test_info(self):
        self.assertEqual("bot-bottle: hello\n", _capture(log.info, "hello"))

    def test_warn(self):
        self.assertEqual(
            "bot-bottle: warning: careful\n", _capture(log.warn, "careful")
        )

    def test_error(self):
        self.assertEqual(
            "bot-bottle: error: boom\n", _capture(log.error, "boom")
        )


class TestContext(unittest.TestCase):
    def test_appends_sorted_parseable_suffix(self):
        out = _capture(
            log.error, "rpc failed", context={"slug": "abc123", "code": "-32603"}
        )
        # keys sorted: code before slug
        self.assertEqual(
            "bot-bottle: error: rpc failed [code=-32603 slug=abc123]\n", out
        )

    def test_quotes_values_with_whitespace(self):
        out = _capture(
            log.info, "did thing", context={"path": "/a b/c", "ok": "yes"}
        )
        self.assertEqual(
            'bot-bottle: did thing [ok=yes path="/a b/c"]\n', out
        )

    def test_empty_context_is_noop_suffix(self):
        self.assertEqual(
            "bot-bottle: x\n", _capture(log.info, "x", context={})
        )


class TestLevels(unittest.TestCase):
    def test_debug_silent_by_default(self):
        self.assertEqual("", _capture(log.debug, "trace"))

    def test_debug_emits_when_level_lowered(self):
        out = _capture(log.debug, "trace", env={"BOT_BOTTLE_LOG_LEVEL": "debug"})
        self.assertEqual("bot-bottle: debug: trace\n", out)

    def test_error_level_suppresses_info_and_warn(self):
        env = {"BOT_BOTTLE_LOG_LEVEL": "error"}
        self.assertEqual("", _capture(log.info, "i", env=env))
        self.assertEqual("", _capture(log.warn, "w", env=env))
        # error still surfaces — nothing sits above it
        self.assertEqual(
            "bot-bottle: error: e\n", _capture(log.error, "e", env=env)
        )

    def test_unknown_level_falls_back_to_default(self):
        # garbage value → default INFO threshold, so info still prints
        out = _capture(log.info, "i", env={"BOT_BOTTLE_LOG_LEVEL": "loud"})
        self.assertEqual("bot-bottle: i\n", out)

    def test_warning_alias_accepted(self):
        env = {"BOT_BOTTLE_LOG_LEVEL": "warning"}
        self.assertEqual("", _capture(log.info, "i", env=env))
        self.assertEqual(
            "bot-bottle: warning: w\n", _capture(log.warn, "w", env=env)
        )


class TestDie(unittest.TestCase):
    def test_die_still_raises_and_prints_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(log.Die) as cm:
                log.die("fatal thing")
        self.assertEqual("fatal thing", cm.exception.message)
        self.assertIn("bot-bottle: error: fatal thing", buf.getvalue())

    def test_die_surfaces_even_at_error_level(self):
        buf = io.StringIO()
        with mock.patch.dict("os.environ", {"BOT_BOTTLE_LOG_LEVEL": "error"}):
            with contextlib.redirect_stderr(buf):
                with self.assertRaises(log.Die):
                    log.die("still fatal")
        self.assertIn("bot-bottle: error: still fatal", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
