"""Uploading a rootfs to a generic package registry (put_generic).

Kept out of test_publish_images, which is skipped as a whole (#749).
"""

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from bot_bottle import publish_images as pi


class PutGeneric(unittest.TestCase):
    def _body(self, tmp: str) -> Path:
        p = Path(tmp) / "rootfs.gz"
        p.write_bytes(b"x" * 32)
        return p

    def test_uploads(self) -> None:
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.Mock(status=201)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
                pi.put_generic("https://x/y", self._body(tmp), "tok")
        # The registry stores and hashes a big rootfs before answering.
        self.assertEqual(pi._UPLOAD_TIMEOUT_SECONDS, urlopen.call_args.kwargs["timeout"])

    def test_no_answer_in_time_is_a_publish_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen",
                            side_effect=TimeoutError("The read operation timed out")):
                with self.assertRaisesRegex(pi.PublishError, "no answer within"):
                    pi.put_generic("https://x/y", self._body(tmp), "tok")

    def test_conflict_points_at_the_version(self) -> None:
        """A 409 means this version was already published. Overwriting silently
        would let two different builds answer to one version."""
        err = urllib.error.HTTPError("u", 409, "conflict", None, None)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(pi.PublishError) as ctx:
                    pi.put_generic("https://x/y", self._body(tmp), "tok")
        self.assertIn("already published", str(ctx.exception))

    def _ok(self) -> mock.MagicMock:
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.Mock(status=201)
        return resp

    @staticmethod
    def _err(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("u", code, "x", None, None)  # type: ignore[arg-type]

    def test_a_bad_gateway_is_tried_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(pi.time, "sleep"), \
                mock.patch("urllib.request.urlopen",
                           side_effect=[self._err(502), self._ok()]) as urlopen:
            pi.put_generic("https://x/y", self._body(tmp), "tok")
        self.assertEqual(2, urlopen.call_count)

    def test_it_gives_up_after_three_tries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(pi.time, "sleep"), \
                mock.patch("urllib.request.urlopen",
                           side_effect=[self._err(502)] * 3) as urlopen:
            with self.assertRaisesRegex(pi.PublishError, "HTTP 502"):
                pi.put_generic("https://x/y", self._body(tmp), "tok")
        self.assertEqual(3, urlopen.call_count)

    def test_a_conflict_after_a_bad_gateway_means_it_was_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(pi.time, "sleep"), \
                mock.patch("urllib.request.urlopen",
                           side_effect=[self._err(502), self._err(409)]):
            pi.put_generic("https://x/y", self._body(tmp), "tok")

    def test_another_error_is_not_tried_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("urllib.request.urlopen",
                           side_effect=self._err(401)) as urlopen:
            with self.assertRaisesRegex(pi.PublishError, "HTTP 401"):
                pi.put_generic("https://x/y", self._body(tmp), "tok")
        self.assertEqual(1, urlopen.call_count)

    def test_unreachable_registry_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.URLError("down")):
                with self.assertRaises(pi.PublishError):
                    pi.put_generic("https://x/y", self._body(tmp), "tok")


if __name__ == "__main__":
    unittest.main()
