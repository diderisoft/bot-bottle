"""PRD 0085 tier-2: overlay Dockerfile synthesis + content-addressed cache tag."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle import agent_overlay as ao
from bot_bottle.manifest import ManifestImageOverlay

_BASE = "reg/o/bot-bottle-claude@sha256:" + "a" * 64


class RenderOverlay(unittest.TestCase):
    def test_from_only_when_empty(self) -> None:
        df = ao.render_overlay_dockerfile(_BASE, ManifestImageOverlay())
        self.assertEqual(df.strip(), f"FROM {_BASE}")

    def test_apt_and_files(self) -> None:
        overlay = ManifestImageOverlay(apt=("jq", "curl"), files=("extra/s.json",))
        df = ao.render_overlay_dockerfile(_BASE, overlay)
        self.assertIn(f"FROM {_BASE}", df)
        self.assertIn("apt-get install -y --no-install-recommends jq curl", df)
        self.assertIn("rm -rf /var/lib/apt/lists/*", df)
        self.assertIn(f"COPY extra/s.json {ao.OVERLAY_DEST}/extra/s.json", df)

    def test_apt_package_is_shell_quoted(self) -> None:
        df = ao.render_overlay_dockerfile(_BASE, ManifestImageOverlay(apt=("a b;rm",)))
        self.assertIn("'a b;rm'", df)

    def test_harness_version_not_yet_supported(self) -> None:
        with self.assertRaises(ao.OverlayError):
            ao.render_overlay_dockerfile(_BASE, ManifestImageOverlay(harness_version="2.1"))


class StageContext(unittest.TestCase):
    def test_writes_dockerfile_and_copies_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            (root / "extra").mkdir(parents=True)
            (root / "extra" / "s.json").write_text("{}")
            dest = Path(tmp) / "ctx"
            overlay = ManifestImageOverlay(apt=("jq",), files=("extra/s.json",))
            df = ao.stage_overlay_context(_BASE, overlay, source_root=root, dest=dest)
            self.assertTrue(df.is_file())
            self.assertIn(f"FROM {_BASE}", df.read_text())
            self.assertTrue((dest / "extra" / "s.json").is_file())

    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            dest = Path(tmp) / "ctx"
            overlay = ManifestImageOverlay(files=("nope.txt",))
            with self.assertRaises(ao.OverlayError):
                ao.stage_overlay_context(_BASE, overlay, source_root=root, dest=dest)


class CacheTag(unittest.TestCase):
    def test_stable_for_same_input(self) -> None:
        o = ManifestImageOverlay(apt=("jq",))
        self.assertEqual(ao.overlay_cache_tag("sha256:x", o), ao.overlay_cache_tag("sha256:x", o))

    def test_changes_with_base_digest(self) -> None:
        o = ManifestImageOverlay(apt=("jq",))
        self.assertNotEqual(ao.overlay_cache_tag("sha256:x", o), ao.overlay_cache_tag("sha256:y", o))

    def test_changes_with_overlay_spec(self) -> None:
        a = ao.overlay_cache_tag("sha256:x", ManifestImageOverlay(apt=("jq",)))
        b = ao.overlay_cache_tag("sha256:x", ManifestImageOverlay(apt=("jq", "curl")))
        c = ao.overlay_cache_tag("sha256:x", ManifestImageOverlay(files=("f",)))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
