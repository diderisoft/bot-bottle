"""Unit: shared cross-backend helpers in backend/util.py."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.backend import util as backend_util


class TestPollCaCert(unittest.TestCase):
    def test_returns_pem_on_first_success(self) -> None:
        result = backend_util.poll_ca_cert(lambda: "PEM", timeout=1.0)
        self.assertEqual("PEM", result)

    def test_raises_timeout_error_when_cert_never_appears(self) -> None:
        with self.assertRaises(TimeoutError):
            backend_util.poll_ca_cert(lambda: None, timeout=0.0)

    def test_polls_until_cert_appears(self) -> None:
        responses = iter([None, None, "-----BEGIN CERTIFICATE-----\n"])
        with patch("bot_bottle.backend.util.time.sleep") as mock_sleep:
            result = backend_util.poll_ca_cert(lambda: next(responses), timeout=5.0)
        self.assertTrue(result.startswith("-----BEGIN CERTIFICATE-----"))
        self.assertEqual(2, mock_sleep.call_count)


if __name__ == "__main__":
    unittest.main()
