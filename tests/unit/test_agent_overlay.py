"""PRD 0085 tier-2: overlay Dockerfile synthesis, validation, staging and cache key."""

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
        df = ao.render_overlay_dockerfile(_BASE, ManifestImageOverlay(), harness="claude")
        self.assertEqual(df.strip(), f"FROM {_BASE}")

    def test_apt_and_files(self) -> None:
        overlay = ManifestImageOverlay(apt=("jq", "curl"), files=("extra/s.json",))
        df = ao.render_overlay_dockerfile(_BASE, overlay, harness="claude")
        self.assertIn(f"FROM {_BASE}", df)
        self.assertIn("apt-get install -y --no-install-recommends jq curl", df)
        self.assertIn("rm -rf /var/lib/apt/lists/*", df)
        self.assertIn(f'COPY ["extra/s.json", "{ao.OVERLAY_DEST}/extra/s.json"]', df)

    def test_copy_uses_json_form(self) -> None:
        # COPY doesn't do shell quoting, so a space in a path must not split it.
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(files=("my file.txt",)), harness="claude")
        self.assertIn(f'COPY ["my file.txt", "{ao.OVERLAY_DEST}/my file.txt"]', df)

    def test_apt_runs_as_root_and_restores_image_user(self) -> None:
        # The default images end as `USER node`; apt needs root, and the agent
        # must not be left running as root afterwards.
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(apt=("jq",), files=("f",)), harness="claude")
        lines = df.splitlines()
        self.assertLess(lines.index("USER root"),
                        next(i for i, l in enumerate(lines) if "apt-get" in l))
        self.assertEqual(lines[-1], "USER node")

    def test_apt_tolerates_expired_snapshot_release(self) -> None:
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(apt=("jq",)), harness="claude")
        self.assertIn("apt-get -o Acquire::Check-Valid-Until=false update", df)

    def test_files_only_does_not_switch_user(self) -> None:
        df = ao.render_overlay_dockerfile(
            _BASE, ManifestImageOverlay(files=("f",)), harness="claude")
        self.assertNotIn("USER", df)

    def test_invalid_apt_package_rejected_before_rendering(self) -> None:
        # A newline would start a new Dockerfile instruction (e.g. a second FROM).
        for pkg in ("a b;rm", "jq\nFROM alpine", "Jq"):
            with self.subTest(pkg=pkg), self.assertRaises(ao.OverlayError):
                ao.render_overlay_dockerfile(
                    _BASE, ManifestImageOverlay(apt=(pkg,)), harness="claude")



class RenderHarness(unittest.TestCase):
    def _render(self, harness: str, version: str = "2.1.180") -> list[str]:
        overlay = ManifestImageOverlay(harness_version=version, apt=("jq",))
        return ao.render_overlay_dockerfile(_BASE, overlay, harness=harness).splitlines()

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
                _BASE, overlay, harness="claude", source_root=root, dest=dest)
            self.assertTrue(df.is_file())
            self.assertIn(f"FROM {_BASE}", df.read_text())
            self.assertTrue((dest / "extra" / "s.json").is_file())

    def test_symlink_out_of_the_launch_dir_is_refused(self) -> None:
        # The launch dir may be an untrusted checkout: a link to a host secret
        # must not be copied into the image.
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            secret.write_text("key")
            root = Path(tmp) / "src"
            (root / "dir").mkdir(parents=True)
            (root / "creds").symlink_to(secret)
            (root / "linked").symlink_to(Path(tmp))
            for rel in ("creds", "linked/secret"):
                with self.subTest(rel=rel), self.assertRaises(ao.OverlayError):
                    ao.stage_overlay_context(
                        _BASE, ManifestImageOverlay(files=(rel,)), harness="claude",
                        source_root=root, dest=Path(tmp) / f"ctx-{rel.replace('/', '-')}")

    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            root.mkdir()
            dest = Path(tmp) / "ctx"
            overlay = ManifestImageOverlay(files=("nope.txt",))
            with self.assertRaises(ao.OverlayError):
                ao.stage_overlay_context(
                _BASE, overlay, harness="claude", source_root=root, dest=dest)


class RenderDockerfileOverlay(unittest.TestCase):
    def _render(self, text: str) -> str:
        return ao.render_overlay_dockerfile_text(_BASE, text, harness="claude")

    def test_pins_base_and_restores_user(self) -> None:
        out = self._render("ARG BASE\nFROM ${BASE}\nUSER root\nRUN apt-get install -y rg\n")
        lines = out.splitlines()
        self.assertIn(f"ARG BASE={_BASE}", lines)
        self.assertLess(lines.index(f"ARG BASE={_BASE}"), lines.index("FROM ${BASE}"))
        self.assertNotIn("ARG BASE", lines)  # the user's bare declaration is replaced
        self.assertEqual("USER node", lines[-1])

    def test_dollar_base_and_stage_name_accepted(self) -> None:
        for frm in ("FROM $BASE", "FROM ${BASE} AS layer", "from ${base}"):
            with self.subTest(frm=frm):
                self.assertIn(frm, self._render(frm + "\nRUN true\n"))

    def test_comments_and_heredocs_accepted(self) -> None:
        out = self._render(
            "FROM ${BASE}\n# set foo=bar later\nRUN <<EOF\necho hi\nEOF\n"
            "COPY <<'END-OF' /etc/x\nhello\nEND-OF\n")
        self.assertEqual("USER node", out.splitlines()[-1])

    def test_continuation_lines_are_one_instruction(self) -> None:
        out = self._render("FROM ${BASE}\nRUN apt-get update && \\\n    apt-get install -y jq\n")
        self.assertIn("    apt-get install -y jq", out)

    def test_rejects(self) -> None:
        cases = {
            "another base": "FROM debian:bookworm\n",
            "multi-stage": "FROM ${BASE}\nFROM ${BASE}\n",
            "no FROM": "RUN true\n",
            "BASE given a value": "ARG BASE=evil/image\nFROM ${BASE}\n",
            "instruction before FROM": "RUN true\nFROM ${BASE}\n",
            "FROM flags": "FROM --platform=linux/amd64 ${BASE}\n",
            # Ways to slip past a line-based check that Docker parses differently:
            "tab after a second FROM": "FROM ${BASE}\nRUN true\nFROM\tscratch\n",
            "syntax directive": "# syntax=evil/frontend\nFROM ${BASE}\n",
            "escape directive": "# escape=`\nFROM ${BASE}\nRUN echo hi \\\nFROM scratch\n",
            "BASE among several ARGs": "ARG X=1 BASE=other:tag\nFROM ${BASE}\n",
            "BASE across a continuation": "ARG \\\n BASE\nFROM ${BASE}\n",
            "trailing continuation": "FROM ${BASE}\nRUN echo hi \\\n",
            "continuation past blanks/comments": "FROM ${BASE}\nRUN echo hi \\\n\n# c\n",
            "unterminated heredoc": "FROM ${BASE}\nRUN <<EOF\necho hi\n",
            "heredoc closed by a prefix": "FROM ${BASE}\nRUN <<END-OF\nEND\n",
            "FROM inside a heredoc": "FROM ${BASE}\nRUN <<EOF\nFROM scratch\nEOF\n",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(ao.OverlayError):
                self._render(text)

    def test_overlay_error_reports_as_a_manifest_error(self) -> None:
        # The CLI prints ManifestErrors as config problems rather than crashing.
        from bot_bottle.manifest import ManifestError
        with self.assertRaises(ManifestError):
            self._render("FROM debian\n")


class StageDockerfileOverlay(unittest.TestCase):
    def test_context_is_the_dockerfile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "src"
            ctx_dir = root / "overlay"
            (ctx_dir / "conf").mkdir(parents=True)
            (ctx_dir / "Dockerfile").write_text("FROM ${BASE}\nCOPY conf/a.json /opt/a.json\n")
            (ctx_dir / "conf" / "a.json").write_text("{}")
            (root / "outside.txt").write_text("not context")
            dest = Path(tmp) / "ctx"
            df = ao.stage_overlay_context(
                _BASE, ManifestImageOverlay(dockerfile="overlay/Dockerfile"),
                harness="claude", source_root=root, dest=dest)
            self.assertEqual(ao.STAGED_DOCKERFILE, df.name)
            self.assertIn(f"ARG BASE={_BASE}", df.read_text())
            self.assertTrue((dest / "conf" / "a.json").is_file())
            self.assertFalse((dest / "outside.txt").exists())

    def test_context_symlinks_are_copied_as_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            secret.write_text("key")
            ctx_dir = Path(tmp) / "src" / "overlay"
            ctx_dir.mkdir(parents=True)
            (ctx_dir / "Dockerfile").write_text("FROM ${BASE}\n")
            (ctx_dir / "creds").symlink_to(secret)
            (ctx_dir / "dangling").symlink_to(Path(tmp) / "nope")
            dest = Path(tmp) / "ctx"
            ao.stage_overlay_context(
                _BASE, ManifestImageOverlay(dockerfile="overlay/Dockerfile"),
                harness="claude", source_root=Path(tmp) / "src", dest=dest)
            self.assertTrue((dest / "creds").is_symlink())
            self.assertTrue((dest / "dangling").is_symlink())

    def test_missing_dockerfile_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ao.OverlayError):
                ao.stage_overlay_context(
                    _BASE, ManifestImageOverlay(dockerfile="nope/Dockerfile"),
                    harness="claude", source_root=Path(tmp), dest=Path(tmp) / "ctx")

    def test_inline_dockerfile_staged_without_source_root(self) -> None:
        # Inline content needs no host file; source_root is not consulted.
        content = "FROM ${BASE}\nRUN echo hi\n"
        with tempfile.TemporaryDirectory() as tmp:
            empty_root = Path(tmp) / "empty"
            empty_root.mkdir()
            dest = Path(tmp) / "ctx"
            df = ao.stage_overlay_context(
                _BASE, ManifestImageOverlay(dockerfile=content),
                harness="claude", source_root=empty_root, dest=dest)
            self.assertTrue(df.is_file())
            text = df.read_text()
            self.assertIn(f"ARG BASE={_BASE}", text)
            self.assertIn("RUN echo hi", text)
            self.assertEqual("USER node", text.splitlines()[-1])

    def test_inline_dockerfile_no_extra_context_files(self) -> None:
        # Only the staged Dockerfile is written; no copytree of a source dir.
        content = "FROM ${BASE}\nRUN true\n"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ctx"
            ao.stage_overlay_context(
                _BASE, ManifestImageOverlay(dockerfile=content),
                harness="claude", source_root=Path(tmp), dest=dest)
            files = list(dest.iterdir())
            self.assertEqual([dest / ao.STAGED_DOCKERFILE], files)

    def test_inline_dockerfile_rejected_content_raises_at_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ao.OverlayError):
                ao.stage_overlay_context(
                    _BASE, ManifestImageOverlay(dockerfile="FROM debian:bookworm\nRUN true\n"),
                    harness="claude", source_root=Path(tmp), dest=Path(tmp) / "ctx")


class StagedOverlayKey(unittest.TestCase):
    def _key(self, root: Path, overlay: ManifestImageOverlay, base: str = _BASE) -> str:
        with ao.staged_overlay(base, overlay, harness="claude", source_root=root) as st:
            self.assertTrue(st.dockerfile.is_file())
            return st.cache_key

    def test_stable_for_same_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o = ManifestImageOverlay(apt=("jq",))
            self.assertEqual(self._key(Path(tmp), o), self._key(Path(tmp), o))

    def test_changes_with_base_spec_and_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "f.txt").write_text("one")
            with_file = ManifestImageOverlay(apt=("jq",), files=("f.txt",))
            keys = {
                self._key(root, ManifestImageOverlay(apt=("jq",))),
                self._key(root, ManifestImageOverlay(apt=("jq", "curl"))),
                self._key(root, ManifestImageOverlay(apt=("jq",)), base=_BASE[:-1] + "b"),
                self._key(root, with_file),
            }
            (root / "f.txt").write_text("two")
            keys.add(self._key(root, with_file))
            self.assertEqual(5, len(keys))

    def test_changes_with_inline_dockerfile_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            o1 = ManifestImageOverlay(dockerfile="FROM ${BASE}\nRUN echo a\n")
            o2 = ManifestImageOverlay(dockerfile="FROM ${BASE}\nRUN echo b\n")
            self.assertNotEqual(self._key(Path(tmp), o1), self._key(Path(tmp), o2))

    def test_changes_with_dockerfile_context_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ov").mkdir()
            (root / "ov" / "Dockerfile").write_text("FROM ${BASE}\n")
            (root / "ov" / "data.txt").write_text("a")
            o = ManifestImageOverlay(dockerfile="ov/Dockerfile")
            before = self._key(root, o)
            (root / "ov" / "data.txt").write_text("b")
            after_data = self._key(root, o)
            # A change to how bot-bottle renders the Dockerfile rebuilds too.
            with mock.patch.object(ao, "get_provider",
                                   return_value=mock.Mock(image_user="other")):
                after_render = self._key(root, o)
            self.assertEqual(3, len({before, after_data, after_render}))

    def test_symlink_target_is_part_of_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ov").mkdir()
            (root / "ov" / "Dockerfile").write_text("FROM ${BASE}\n")
            (root / "ov" / "link").symlink_to("a")
            o = ManifestImageOverlay(dockerfile="ov/Dockerfile")
            before = self._key(root, o)
            (root / "ov" / "link").unlink()
            (root / "ov" / "link").symlink_to("b")
            self.assertNotEqual(before, self._key(root, o))

    def test_missing_input_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for o in (ManifestImageOverlay(files=("x",)),
                      ManifestImageOverlay(dockerfile="x/Dockerfile")):
                with self.subTest(o=o), self.assertRaises(ao.OverlayError):
                    self._key(Path(tmp), o)


class InputsDigest(unittest.TestCase):
    """The trust hash's view of an overlay: the cache key's staged context,
    without the base."""

    def _both(self, root: Path, o: ManifestImageOverlay, base: str = _BASE) -> tuple[str, str]:
        with ao.staged_overlay(base, o, harness="claude", source_root=root) as st:
            key = st.cache_key
        return key, ao.inputs_digest(o, harness="claude", source_root=root)

    def test_follows_the_cache_key_except_for_the_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ov").mkdir()
            (root / "ov" / "Dockerfile").write_text("FROM ${BASE}\n")
            o = ManifestImageOverlay(dockerfile="ov/Dockerfile")
            key1, digest1 = self._both(root, o)
            key2, digest2 = self._both(root, o, base=_BASE[:-1] + "b")
            self.assertNotEqual(key1, key2)
            self.assertEqual(digest1, digest2)
            (root / "ov" / "extra").write_text("x")
            key3, digest3 = self._both(root, o)
            self.assertNotEqual(key1, key3)
            self.assertNotEqual(digest1, digest3)

    def test_empty_overlay_digests_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual("", ao.inputs_digest(
                ManifestImageOverlay(), harness="claude", source_root=Path(tmp)))

    def test_an_unstageable_overlay_digests_its_error_stably(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            o = ManifestImageOverlay(files=("x",))
            missing = ao.inputs_digest(o, harness="claude", source_root=root)
            self.assertEqual(missing, ao.inputs_digest(o, harness="claude", source_root=root))
            (root / "x").write_text("now here")
            self.assertNotEqual(missing,
                                ao.inputs_digest(o, harness="claude", source_root=root))


class NoPublishedBase(unittest.TestCase):
    def test_message_names_the_template(self) -> None:
        err = ao.no_published_base_error("claude")
        self.assertIsInstance(err, ao.OverlayError)
        self.assertIn("'claude'", str(err))


if __name__ == "__main__":
    unittest.main()
