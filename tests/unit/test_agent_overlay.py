"""PRD 0085 tier-2: overlay Dockerfile synthesis + content-addressed cache tag."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import agent_overlay as ao
from bot_bottle.manifest import ManifestImageOverlay

_BASE = "reg/o/bot-bottle-claude@sha256:" + "a" * 64


class RenderOverlay(unittest.TestCase):
    def test_from_only_when_empty(self) -> None:
        df = ao.render_overlay_dockerfile(_BASE, ManifestImageOverlay(), template="claude")
        self.assertEqual(df.strip(), f"FROM {_BASE}")

    def test_apt_and_files(self) -> None:
        overlay = ManifestImageOverlay(apt=("jq", "curl"), files=("extra/s.json",))
        df = ao.render_overlay_dockerfile(_BASE, overlay, template="claude")
        self.assertIn(f"FROM {_BASE}", df)
        self.assertIn("apt-get install -y --no-install-recommends jq curl", df)
        self.assertIn("rm -rf /var/lib/apt/lists/*", df)
        self.assertIn(f"COPY extra/s.json {ao.OVERLAY_DEST}/extra/s.json", df)

    def test_apt_runs_as_root_and_restores_image_user(self) -> None:
        # The default images end as `USER node`; apt needs root, and the agent
        # must not be left running as root afterwards.
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(apt=("jq",), files=("f",)), template="claude")
        lines = df.splitlines()
        self.assertLess(lines.index("USER root"),
                        next(i for i, l in enumerate(lines) if "apt-get" in l))
        self.assertEqual(lines[-1], "USER node")

    def test_apt_tolerates_expired_snapshot_release(self) -> None:
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(apt=("jq",)), template="claude")
        self.assertIn("apt-get -o Acquire::Check-Valid-Until=false update", df)

    def test_files_only_does_not_switch_user(self) -> None:
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(files=("f",)), template="claude")
        self.assertNotIn("USER", df)

    def test_apt_package_is_shell_quoted(self) -> None:
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(apt=("a b;rm",)), template="claude")
        self.assertIn("'a b;rm'", df)



class RenderHarness(unittest.TestCase):
    def _render(self, template: str, version: str = "2.1.180") -> list[str]:
        overlay = ManifestImageOverlay(harness_version=version, apt=("jq",))
        return ao.render_overlay_dockerfile(_BASE, overlay, template=template).splitlines()

    def _assert_bracketed_by_users(self, lines: list[str], marker: str) -> None:
        # Harness steps run after the switch to root and before the image
        # user is restored, so the agent never ends up running as root.
        self.assertLess(lines.index("USER root"),
                        next(i for i, l in enumerate(lines) if marker in l))
        self.assertEqual(lines[-1], "USER node")

    def test_claude_installs_and_checks_version(self) -> None:
        lines = self._render("claude")
        run = next(l for l in lines if "claude-code@" in l)
        self.assertIn("cd /opt/claude", run)
        self.assertIn("npm install --omit=dev --no-save", run)
        self.assertIn("@anthropic-ai/claude-code@2.1.180", run)
        self.assertIn('test "$(claude --version | cut -d\' \' -f1)" = 2.1.180', run)
        self._assert_bracketed_by_users(lines, "claude-code@")

    def test_pi_bumps_core_packages_together(self) -> None:
        lines = self._render("pi", "0.82.0")
        run = next(l for l in lines if "pi-coding-agent@" in l)
        for pkg in ("pi-coding-agent", "pi-agent-core", "pi-ai", "pi-tui"):
            self.assertIn(f"@earendil-works/{pkg}@0.82.0", run)
        self.assertIn("cd /home/node/.pi/agent", run)
        self.assertIn('test "$(pi --version)" = 0.82.0', run)
        self.assertEqual(lines[lines.index(run) - 1], "USER node")
        self._assert_bracketed_by_users(lines, "pi-coding-agent@")

    def test_codex_verifies_against_upstream_checksums(self) -> None:
        lines = self._render("codex", "0.146.0")
        run = next(l for l in lines if "codex-package" in l)
        release = "https://github.com/openai/codex/releases/download/rust-v0.146.0"
        self.assertIn(f"{release}/codex-package_SHA256SUMS", run)
        self.assertIn("sha256sum -c -", run)
        self.assertIn("releases/0.146.0-${codex_target}", run)
        self.assertIn('test "$(codex --version)" = "codex-cli 0.146.0"', run)
        self._assert_bracketed_by_users(lines, "codex-package")

    def test_apt_precedes_harness(self) -> None:
        lines = self._render("claude")
        self.assertLess(next(i for i, l in enumerate(lines) if "apt-get" in l),
                        next(i for i, l in enumerate(lines) if "claude-code@" in l))

    def test_unsupported_provider_raises(self) -> None:
        provider = mock.Mock(harness_overlay_lines=mock.Mock(return_value=None))
        with mock.patch.object(ao, "get_provider", return_value=provider), \
             self.assertRaises(ao.OverlayError):
            self._render("custom")

    def test_invalid_version_rejected_before_rendering(self) -> None:
        with self.assertRaises(ao.OverlayError):
            self._render("claude", "2.1.180 && curl evil")


class StageContext(unittest.TestCase):
    def test_writes_dockerfile_and_copies_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            (root / "extra").mkdir(parents=True)
            (root / "extra" / "s.json").write_text("{}")
            dest = Path(tmp) / "ctx"
            overlay = ManifestImageOverlay(apt=("jq",), files=("extra/s.json",))
            df = ao.stage_overlay_context(
                _BASE, overlay, template="claude", source_root=root, dest=dest)
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
                ao.stage_overlay_context(
                _BASE, overlay, template="claude", source_root=root, dest=dest)


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
        d = ao.overlay_cache_tag("sha256:x", ManifestImageOverlay(
            apt=("jq",), harness_version="2.1.180"))
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)


if __name__ == "__main__":
    unittest.main()
