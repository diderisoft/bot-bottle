"""A failed image build says why in one line, not as a CalledProcessError's
argv dump."""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from bot_bottle.lib import log
from bot_bottle.lib.log import Die
from bot_bottle.lib.proc import build_failure, run_streaming

# Trimmed from a real nested-containers build on macos-container.
_APT_EXPIRED = """\
#10 0.607 Get:1 http://snapshot.debian.org/archive/debian/20260724T000000Z trixie InRelease [140 kB]
#10 3.908 E: Release file for http://snapshot.debian.org/archive/debian/20260724T000000Z/dists/trixie-updates/InRelease is expired (invalid since 92d 0h 31min 48s). Updates for this repository will not be applied.
#10 3.908 E: Release file for http://snapshot.debian.org/archive/debian-security/20260724T000000Z/dists/trixie-security/InRelease is expired (invalid since 91d 23h 26min 19s). Updates for this repository will not be applied.
#10 ERROR: process "/bin/sh -c apt-get update" did not complete successfully: exit code: 100
------
3.908 E: Release file for http://snapshot.debian.org/archive/debian/20260724T000000Z/dists/trixie-updates/InRelease is expired (invalid since 92d 0h 31min 48s). Updates for this repository will not be applied.
------
Error: unknown: "failed to solve: process did not complete successfully: exit code: 100"
"""


def _failed(output: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["container", "build"], output=output)


class TestBuildFailure(unittest.TestCase):
    def test_leads_with_apts_own_error(self) -> None:
        message = build_failure("agent:nested", _failed(_APT_EXPIRED))
        self.assertTrue(message.startswith(
            "building agent:nested failed: E: Release file for "
            "http://snapshot.debian.org/archive/debian/20260724T000000Z/dists/trixie-updates"))
        self.assertIn("is expired", message)

    def test_else_the_last_error_line(self) -> None:
        output = "#5 RUN npm ci\n#5 ERROR: npm ci exited 1\nError: unknown: failed to solve\n"
        self.assertEqual("building a:b failed: Error: unknown: failed to solve",
                         build_failure("a:b", _failed(output)))

    def test_points_at_the_output_when_there_is_nothing_to_quote(self) -> None:
        self.assertEqual("building a:b failed (exit 1); see its build output",
                         build_failure("a:b", _failed("")))

    def test_the_streamed_tail_rides_on_the_error(self) -> None:
        script = "import sys; print('E: broken'); sys.exit(3)"
        with mock.patch.object(log, "redirected", return_value=True), \
                mock.patch.object(log, "write_line") as write:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                run_streaming([sys.executable, "-c", script], check=True)
        write.assert_called_once_with("E: broken")
        self.assertEqual("E: broken", caught.exception.output)

    def test_both_backends_die_with_it(self) -> None:
        from bot_bottle.backend.docker import util as docker_util
        from bot_bottle.backend.macos_container import util as macos_util
        for build in (lambda: docker_util.build_image("a:b", "/ctx"),
                      lambda: macos_util.build_image("a:b", "/ctx")):
            with self.subTest(), \
                    mock.patch.object(docker_util, "run_streaming",
                                      side_effect=_failed("E: nope")), \
                    mock.patch.object(macos_util, "run_streaming",
                                      side_effect=_failed("E: nope")), \
                    mock.patch.object(macos_util, "_ensure_builder_dns"), \
                    mock.patch.object(macos_util, "dns_server", return_value="1.1.1.1"), \
                    mock.patch.object(log, "error"):
                with self.assertRaises(Die) as caught:
                    build()
            self.assertEqual("building a:b failed: E: nope", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
