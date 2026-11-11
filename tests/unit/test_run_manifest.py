"""A bottled agent's manifest versions."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import run_manifest
from bot_bottle.manifest import ManifestBottle


class TestRunManifest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(Path(tmp.name))})
        env.start()
        self.addCleanup(env.stop)

    def test_versions(self) -> None:
        self.assertIsNone(run_manifest.latest("s1"))
        self.assertEqual(1, run_manifest.record("s1", {"env": {"A": "b"}}, "launched"))
        self.assertEqual(2, run_manifest.record("s1", {"env": {"A": "c"}}, "saved"))
        entry, doc = run_manifest.latest("s1")  # type: ignore[misc]
        self.assertEqual((2, "saved", {"env": {"A": "c"}}), (entry["version"], entry["message"], doc))
        self.assertEqual({"env": {"A": "b"}}, run_manifest.read("s1", 1))
        self.assertEqual([1, 2], [v["version"] for v in run_manifest.versions("s1")])

    def test_launch_and_resume(self) -> None:
        run_manifest.record_launch("s1", ManifestBottle(env={"A": "b"}))
        run_manifest.record_launch("s1", ManifestBottle(env={"A": "b"}))  # resumed, same
        self.assertEqual(["launched"], [v["message"] for v in run_manifest.versions("s1")])
        run_manifest.record_launch("s1", ManifestBottle(env={"A": "c"}))  # files changed
        self.assertEqual("resumed with the bottle files as they are now",
                         run_manifest.versions("s1")[-1]["message"])

    def test_the_log_and_the_launch_are_host_db_rows(self) -> None:
        run_manifest.record_launch("s1", ManifestBottle(env={"A": "b"}), host_version="abc")
        self.assertEqual({"bottles": {}, "host": "abc"}, run_manifest.launch_record("s1"))
        manifest_dir = Path(os.environ["BOT_BOTTLE_ROOT"]) / "state" / "s1" / "manifest"
        self.assertEqual(["v0001.yaml"], sorted(p.name for p in manifest_dir.iterdir()))
        self.assertEqual({}, run_manifest.launch_record("s2"))

    def test_a_document_that_cant_be_written_adds_no_version(self) -> None:
        with mock.patch.object(Path, "write_text", side_effect=OSError("ro")), \
                self.assertRaises(OSError):
            run_manifest.record("s1", {"env": {}}, "launched")
        self.assertEqual([], run_manifest.versions("s1"))

    def test_launch_never_raises(self) -> None:
        with mock.patch.object(run_manifest, "record", side_effect=OSError("ro")):
            run_manifest.record_launch("s1", ManifestBottle())
        self.assertEqual([], run_manifest.versions("s1"))


if __name__ == "__main__":
    unittest.main()
