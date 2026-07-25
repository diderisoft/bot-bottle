"""Unit tests for git_gate_host_key (issue #333)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.git_gate.host_key import (
    add_host_key_to_frontmatter,
    find_and_update_bottle_file,
    find_repo_bottle_file,
    prompt_tty,
    fetch_host_key,
    preflight_host_keys,
)
from bot_bottle.manifest import Manifest, ManifestIndex


# ---------------------------------------------------------------------------
# fetch_host_key
# ---------------------------------------------------------------------------


class TestFetchHostKey(unittest.TestCase):
    def _run_result(self, stdout: str, returncode: int = 0) -> MagicMock:
        r = MagicMock()
        r.stdout = stdout
        r.stderr = ""
        r.returncode = returncode
        return r

    def test_returns_type_and_key(self) -> None:
        stdout = "gitea.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("gitea.example.com", "22")
        self.assertEqual("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA", key)

    def test_non_default_port_passes_p_flag(self) -> None:
        stdout = "[gitea.example.com]:30009 ssh-ed25519 AAAA\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)) as mock_run:
            fetch_host_key("gitea.example.com", "30009")
        args = mock_run.call_args[0][0]
        self.assertIn("-p", args)
        self.assertIn("30009", args)

    def test_default_port_omits_p_flag(self) -> None:
        stdout = "github.com ssh-ed25519 AAAA\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)) as mock_run:
            fetch_host_key("github.com", "22")
        args = mock_run.call_args[0][0]
        self.assertNotIn("-p", args)

    def test_skips_comment_lines(self) -> None:
        stdout = "# comment\ngithub.com ssh-ed25519 AAAA\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("github.com", "22")
        self.assertEqual("ssh-ed25519 AAAA", key)

    def test_skips_lines_without_three_parts(self) -> None:
        # A line with only two whitespace-separated tokens is not a valid
        # known_hosts entry; it should be skipped and the function should
        # raise rather than return a partial result.
        stdout = "malformed-line\ngithub.com ssh-ed25519 AAAA\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("github.com", "22")
        self.assertEqual("ssh-ed25519 AAAA", key)

    def test_raises_on_empty_output(self) -> None:
        with patch("subprocess.run", return_value=self._run_result("")):
            with self.assertRaises(RuntimeError) as cm:
                fetch_host_key("example.com", "22")
        self.assertIn("no host key", str(cm.exception))

    def test_prefers_ed25519_over_ecdsa(self) -> None:
        stdout = (
            "gitea.example.com ecdsa-sha2-nistp256 BBBBB\n"
            "gitea.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA\n"
        )
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("gitea.example.com", "22")
        self.assertEqual("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA", key)

    def test_falls_back_to_ecdsa_without_ed25519(self) -> None:
        stdout = "gitea.example.com ecdsa-sha2-nistp256 BBBBB\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("gitea.example.com", "22")
        self.assertEqual("ecdsa-sha2-nistp256 BBBBB", key)

    def test_falls_back_to_rsa_without_ed25519_or_ecdsa(self) -> None:
        stdout = "gitea.example.com ssh-rsa AAAAB3NzaC1yc2EAAAA\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("gitea.example.com", "22")
        self.assertEqual("ssh-rsa AAAAB3NzaC1yc2EAAAA", key)

    def test_falls_back_to_first_for_unknown_type(self) -> None:
        stdout = "gitea.example.com ssh-unknown CCCCC\n"
        with patch("subprocess.run", return_value=self._run_result(stdout)):
            key = fetch_host_key("gitea.example.com", "22")
        self.assertEqual("ssh-unknown CCCCC", key)

    def test_raises_includes_stderr_when_present(self) -> None:
        r = self._run_result("")
        r.stderr = "connection refused"
        with patch("subprocess.run", return_value=r):
            with self.assertRaises(RuntimeError) as cm:
                fetch_host_key("example.com", "22")
        self.assertIn("connection refused", str(cm.exception))

    def test_raises_on_os_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError("not found")):
            with self.assertRaises(RuntimeError) as cm:
                fetch_host_key("example.com", "22")
        self.assertIn("could not launch", str(cm.exception))

    def test_raises_on_timeout(self) -> None:
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh-keyscan", 15)):
            with self.assertRaises(RuntimeError) as cm:
                fetch_host_key("example.com", "22")
        self.assertIn("timed out", str(cm.exception))


# ---------------------------------------------------------------------------
# prompt_tty
# ---------------------------------------------------------------------------


class TestPromptTty(unittest.TestCase):
    def test_reads_from_tty_when_available(self) -> None:
        import io
        fake_tty = io.StringIO("yes\n")
        with patch("builtins.open", return_value=fake_tty), patch("sys.stderr"):
            result = prompt_tty("question: ")
        self.assertEqual("yes", result)

    def test_falls_back_to_stdin_on_os_error(self) -> None:
        import io
        with patch("builtins.open", side_effect=OSError("no tty")), \
                patch("sys.stdin", io.StringIO("fallback\n")), \
                patch("sys.stderr"):
            result = prompt_tty("question: ")
        self.assertEqual("fallback", result)


# ---------------------------------------------------------------------------
# add_host_key_to_frontmatter
# ---------------------------------------------------------------------------


class TestAddHostKeyToFrontmatter(unittest.TestCase):
    _FILE = """\
---
git-gate:
  repos:
    myrepo:
      url: ssh://git@host/org/repo.git
      key:
        provider: static
        path: /dev/null
---
# Body text here
"""

    def test_inserts_host_key_at_repo_level(self) -> None:
        result = add_host_key_to_frontmatter(self._FILE, "myrepo", "ssh-ed25519 AAAA")
        self.assertIn("host_key: ssh-ed25519 AAAA", result)
        # Must be a sibling of `url` and `key`, not nested inside `key`
        lines = result.splitlines()
        hk_line = next(l for l in lines if "host_key" in l)
        url_line = next(l for l in lines if "url:" in l)
        self.assertEqual(
            len(hk_line) - len(hk_line.lstrip()),
            len(url_line) - len(url_line.lstrip()),
        )

    def test_preserves_body(self) -> None:
        result = add_host_key_to_frontmatter(self._FILE, "myrepo", "ssh-ed25519 AAAA")
        self.assertIn("# Body text here", result)

    def test_preserves_other_repos(self) -> None:
        text = """\
---
git-gate:
  repos:
    first:
      url: ssh://git@host/org/first.git
      key:
        provider: static
        path: /dev/null
    second:
      url: ssh://git@host/org/second.git
      key:
        provider: static
        path: /dev/null
---
"""
        result = add_host_key_to_frontmatter(text, "first", "ssh-ed25519 AAAA")
        self.assertIn("host_key: ssh-ed25519 AAAA", result)
        self.assertIn("second:", result)

    def test_no_change_when_host_key_already_present(self) -> None:
        text = self._FILE.replace(
            "        path: /dev/null\n",
            "        path: /dev/null\n      host_key: ssh-ed25519 EXISTING\n",
        )
        result = add_host_key_to_frontmatter(text, "myrepo", "ssh-ed25519 NEW")
        self.assertNotIn("NEW", result)

    def test_no_change_when_repo_not_found(self) -> None:
        result = add_host_key_to_frontmatter(self._FILE, "other", "ssh-ed25519 AAAA")
        self.assertEqual(self._FILE, result)

    def test_no_change_without_frontmatter(self) -> None:
        text = "No frontmatter here\n"
        result = add_host_key_to_frontmatter(text, "myrepo", "ssh-ed25519 AAAA")
        self.assertEqual(text, result)

    def test_no_change_when_no_closing_delimiter(self) -> None:
        text = "---\ngit-gate:\n  repos:\n    myrepo:\n      url: x\n"
        result = add_host_key_to_frontmatter(text, "myrepo", "ssh-ed25519 AAAA")
        self.assertEqual(text, result)

    def test_no_change_when_git_gate_missing(self) -> None:
        text = "---\nenv:\n  FOO: bar\n---\n"
        result = add_host_key_to_frontmatter(text, "myrepo", "ssh-ed25519 AAAA")
        self.assertEqual(text, result)

    def test_no_change_when_repos_missing(self) -> None:
        text = "---\ngit-gate:\n  foo: bar\n---\n"
        result = add_host_key_to_frontmatter(text, "myrepo", "ssh-ed25519 AAAA")
        self.assertEqual(text, result)

    def test_no_change_when_repo_entry_not_a_dict(self) -> None:
        text = "---\ngit-gate:\n  repos:\n    - item\n---\n"
        result = add_host_key_to_frontmatter(text, "myrepo", "ssh-ed25519 AAAA")
        self.assertEqual(text, result)


# ---------------------------------------------------------------------------
# find_and_update_bottle_file
# ---------------------------------------------------------------------------


class TestFindAndUpdateBottleFile(unittest.TestCase):
    def _write_bottle(self, d: str, name: str, content: str) -> Path:
        path = Path(d) / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_updates_file_with_matching_repo(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            content = """\
---
git-gate:
  repos:
    myrepo:
      url: ssh://git@host/org/repo.git
      key:
        provider: static
        path: /dev/null
---
"""
            path = self._write_bottle(d, "dev", content)
            result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertTrue(result)
            updated = path.read_text()
            self.assertIn("host_key: ssh-ed25519 AAAA", updated)

    def test_returns_false_when_no_matching_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            content = """\
---
git-gate:
  repos:
    other:
      url: ssh://git@host/org/other.git
      key:
        provider: static
        path: /dev/null
---
"""
            self._write_bottle(d, "dev", content)
            result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertFalse(result)

    def test_skips_file_that_already_has_host_key(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            content = """\
---
git-gate:
  repos:
    myrepo:
      url: ssh://git@host/org/repo.git
      key:
        provider: static
        path: /dev/null
      host_key: ssh-ed25519 EXISTING
---
"""
            path = self._write_bottle(d, "dev", content)
            result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 NEW")
            self.assertFalse(result)
            self.assertNotIn("NEW", path.read_text())

    def test_returns_false_for_nonexistent_dir(self) -> None:
        result = find_and_update_bottle_file(
            Path("/nonexistent/dir"), "myrepo", "ssh-ed25519 AAAA"
        )
        self.assertFalse(result)

    def test_skips_file_without_git_gate_section(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write_bottle(d, "plain", "---\nenv:\n  FOO: bar\n---\n")
            result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertFalse(result)

    def test_skips_file_with_non_dict_repos(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write_bottle(d, "bad", "---\ngit-gate:\n  repos:\n    - item\n---\n")
            result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertFalse(result)

    def test_skips_invalid_utf8_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "broken.md"
            path.write_bytes(b"\xff\xfe")  # invalid UTF-8
            result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertFalse(result)

    def test_skips_unreadable_file(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "unreadable.md"
            path.write_text("---\n---\n")
            os.chmod(path, 0o000)
            try:
                result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
                self.assertFalse(result)
            finally:
                os.chmod(path, 0o644)

    def test_returns_false_when_file_unreadable_on_write(self) -> None:
        # find_repo_bottle_file succeeds, but the second read (for writing)
        # raises OSError — e.g. file removed or permissions changed.
        content = """\
---
git-gate:
  repos:
    myrepo:
      url: ssh://git@host/org/repo.git
      key:
        provider: static
        path: /dev/null
---
"""
        with tempfile.TemporaryDirectory() as d:
            path = self._write_bottle(d, "dev", content)
            with patch(
                "bot_bottle.git_gate.host_key.find_repo_bottle_file",
                return_value=path,
            ), patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
                result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertFalse(result)

    def test_skips_when_update_produces_no_change(self) -> None:
        # add_host_key_to_frontmatter returns the original text unchanged
        # (e.g. because the entry is absent from the parsed structure).
        # find_and_update_bottle_file must continue rather than write.
        content = """\
---
git-gate:
  repos:
    myrepo:
      url: ssh://git@host/org/repo.git
      key:
        provider: static
        path: /dev/null
---
"""
        with tempfile.TemporaryDirectory() as d:
            path = self._write_bottle(d, "dev", content)
            with patch(
                "bot_bottle.git_gate.host_key.add_host_key_to_frontmatter",
                return_value=content,  # no change
            ):
                result = find_and_update_bottle_file(Path(d), "myrepo", "ssh-ed25519 AAAA")
            self.assertFalse(result)
            self.assertNotIn("host_key", path.read_text())


# ---------------------------------------------------------------------------
# find_repo_bottle_file
# ---------------------------------------------------------------------------


class TestFindRepoBottleFile(unittest.TestCase):
    _CONTENT = """\
---
git-gate:
  repos:
    myrepo:
      url: ssh://git@host/org/repo.git
      key:
        provider: static
        path: /dev/null
---
"""

    def _write(self, d: str, name: str, content: str) -> Path:
        path = Path(d) / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_returns_path_when_repo_found(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, "dev", self._CONTENT)
            result = find_repo_bottle_file(Path(d), "myrepo")
            self.assertEqual(path, result)

    def test_returns_none_when_no_matching_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "dev", self._CONTENT)
            self.assertIsNone(find_repo_bottle_file(Path(d), "other"))

    def test_returns_none_when_host_key_already_set(self) -> None:
        content = self._CONTENT.replace("        path: /dev/null\n",
                                        "        path: /dev/null\n      host_key: X\n")
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "dev", content)
            self.assertIsNone(find_repo_bottle_file(Path(d), "myrepo"))

    def test_returns_none_for_nonexistent_dir(self) -> None:
        self.assertIsNone(find_repo_bottle_file(Path("/nonexistent"), "myrepo"))

    def test_skips_unreadable_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "unreadable.md").write_text(self._CONTENT)
            with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
                self.assertIsNone(find_repo_bottle_file(Path(d), "myrepo"))

    def test_skips_file_with_non_dict_git_gate(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dev.md").write_text("---\ngit-gate:\n  - item\n---\n")
            self.assertIsNone(find_repo_bottle_file(Path(d), "myrepo"))

    def test_skips_file_with_non_dict_repos(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "dev.md").write_text("---\ngit-gate:\n  repos:\n    - item\n---\n")
            self.assertIsNone(find_repo_bottle_file(Path(d), "myrepo"))

    def test_save_prompt_includes_filename(self) -> None:
        """preflight prompt must name the discovered bottle file."""
        manifest = _manifest_with_git()

        with tempfile.TemporaryDirectory() as home:
            bottles_dir = Path(home) / "bottles"
            bottles_dir.mkdir()
            bottle_file = bottles_dir / "dev.md"
            bottle_file.write_text(
                "---\ngit-gate:\n  repos:\n    myrepo:\n"
                "      url: ssh://git@gitea.example.com:30009/org/myrepo.git\n"
                "      key:\n        provider: static\n        path: /dev/null\n---\n",
                encoding="utf-8",
            )

            mock_prompt = MagicMock(side_effect=["y", "n"])  # confirm key, skip save
            with patch(
                "bot_bottle.git_gate.host_key.fetch_host_key",
                return_value="ssh-ed25519 FETCHED",
            ), patch(
                "bot_bottle.git_gate.host_key.prompt_tty", mock_prompt,
            ), patch("sys.stderr"):
                preflight_host_keys(manifest, headless=False, home_md=Path(home))

            save_call = next(
                c for c in mock_prompt.call_args_list if "Save" in c.args[0]
            )
            self.assertIn(str(bottle_file), save_call.args[0])


# ---------------------------------------------------------------------------
# preflight_host_keys
# ---------------------------------------------------------------------------


def _manifest_with_git(host_key: str = "") -> Manifest:
    """Build a Manifest with one git entry; host_key defaults to empty."""
    return ManifestIndex.from_json_obj({
        "bottles": {
            "dev": {
                "git-gate": {
                    "repos": {
                        "myrepo": {
                            "url": "ssh://git@gitea.example.com:30009/org/myrepo.git",
                            "key": {"provider": "static", "path": "/dev/null"},
                            **({"host_key": host_key} if host_key else {}),
                        },
                    },
                },
            },
        },
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).load_for_agent("demo")


class TestPreflightHostKeys(unittest.TestCase):
    def test_no_op_when_all_keys_present(self) -> None:
        manifest = _manifest_with_git("ssh-ed25519 AAAA")
        result = preflight_host_keys(manifest, headless=False, home_md=None)
        self.assertIs(result, manifest)

    def test_headless_dies_when_key_missing(self) -> None:
        manifest = _manifest_with_git()
        with self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=True, home_md=None)

    def test_interactive_fetches_and_confirms_key(self) -> None:
        manifest = _manifest_with_git()

        with patch(
            "bot_bottle.git_gate.host_key.fetch_host_key",
            return_value="ssh-ed25519 FETCHED",
        ), patch(
            "bot_bottle.git_gate.host_key.prompt_tty",
            side_effect=["y", "n"],  # confirm key, don't persist
        ), patch("sys.stderr"):
            result = preflight_host_keys(manifest, headless=False, home_md=None)

        self.assertEqual("ssh-ed25519 FETCHED", result.bottle.git[0].KnownHostKey)

    def test_interactive_dies_when_key_not_confirmed(self) -> None:
        manifest = _manifest_with_git()

        with patch(
            "bot_bottle.git_gate.host_key.fetch_host_key",
            return_value="ssh-ed25519 FETCHED",
        ), patch(
            "bot_bottle.git_gate.host_key.prompt_tty",
            return_value="n",
        ), patch("sys.stderr"), self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=False, home_md=None)

    def test_interactive_persists_when_requested(self) -> None:
        manifest = _manifest_with_git()

        with tempfile.TemporaryDirectory() as home:
            bottles_dir = Path(home) / "bottles"
            bottles_dir.mkdir()
            bottle_file = bottles_dir / "dev.md"
            bottle_file.write_text(
                "---\ngit-gate:\n  repos:\n    myrepo:\n"
                "      url: ssh://git@gitea.example.com:30009/org/myrepo.git\n"
                "      key:\n        provider: static\n        path: /dev/null\n---\n",
                encoding="utf-8",
            )

            with patch(
                "bot_bottle.git_gate.host_key.fetch_host_key",
                return_value="ssh-ed25519 FETCHED",
            ), patch(
                "bot_bottle.git_gate.host_key.prompt_tty",
                side_effect=["y", "y"],  # confirm key, yes persist
            ), patch("sys.stderr"):
                result = preflight_host_keys(
                    manifest, headless=False, home_md=Path(home),
                )

            self.assertEqual("ssh-ed25519 FETCHED", result.bottle.git[0].KnownHostKey)
            written = bottle_file.read_text()
            self.assertIn("host_key: ssh-ed25519 FETCHED", written)

    def test_interactive_skips_persist_when_declined(self) -> None:
        manifest = _manifest_with_git()

        with tempfile.TemporaryDirectory() as home:
            bottles_dir = Path(home) / "bottles"
            bottles_dir.mkdir()
            bottle_file = bottles_dir / "dev.md"
            bottle_file.write_text(
                "---\ngit-gate:\n  repos:\n    myrepo:\n"
                "      url: ssh://git@gitea.example.com:30009/org/myrepo.git\n"
                "      key:\n        provider: static\n        path: /dev/null\n---\n",
                encoding="utf-8",
            )

            with patch(
                "bot_bottle.git_gate.host_key.fetch_host_key",
                return_value="ssh-ed25519 FETCHED",
            ), patch(
                "bot_bottle.git_gate.host_key.prompt_tty",
                side_effect=["y", "n"],  # confirm key, no persist
            ), patch("sys.stderr"):
                result = preflight_host_keys(
                    manifest, headless=False, home_md=Path(home),
                )

            self.assertEqual("ssh-ed25519 FETCHED", result.bottle.git[0].KnownHostKey)
            written = bottle_file.read_text()
            self.assertNotIn("host_key", written)

    def test_interactive_dies_when_fetch_fails(self) -> None:
        manifest = _manifest_with_git()

        with patch(
            "bot_bottle.git_gate.host_key.fetch_host_key",
            side_effect=RuntimeError("connection refused"),
        ), patch("sys.stderr"), self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=False, home_md=None)

    def test_interactive_warns_when_persist_file_not_found(self) -> None:
        manifest = _manifest_with_git()

        with tempfile.TemporaryDirectory() as home:
            # bottles/ dir exists but does not contain the repo entry
            bottles_dir = Path(home) / "bottles"
            bottles_dir.mkdir()

            import io
            buf = io.StringIO()
            with patch(
                "bot_bottle.git_gate.host_key.fetch_host_key",
                return_value="ssh-ed25519 FETCHED",
            ), patch(
                "bot_bottle.git_gate.host_key.prompt_tty",
                side_effect=["y"],  # confirm key only; no save prompt when no file found
            ), patch("sys.stderr", buf):
                result = preflight_host_keys(
                    manifest, headless=False, home_md=Path(home),
                )

            self.assertEqual("ssh-ed25519 FETCHED", result.bottle.git[0].KnownHostKey)
            self.assertIn("kept in memory", buf.getvalue())

    def test_interactive_warns_when_write_fails_after_prompt(self) -> None:
        # User says yes to save, but find_and_update_bottle_file fails.
        manifest = _manifest_with_git()

        with tempfile.TemporaryDirectory() as home:
            bottles_dir = Path(home) / "bottles"
            bottles_dir.mkdir()
            bottle_file = bottles_dir / "dev.md"
            bottle_file.write_text(
                "---\ngit-gate:\n  repos:\n    myrepo:\n"
                "      url: ssh://git@gitea.example.com:30009/org/myrepo.git\n"
                "      key:\n        provider: static\n        path: /dev/null\n---\n",
                encoding="utf-8",
            )

            import io
            buf = io.StringIO()
            with patch(
                "bot_bottle.git_gate.host_key.fetch_host_key",
                return_value="ssh-ed25519 FETCHED",
            ), patch(
                "bot_bottle.git_gate.host_key.prompt_tty",
                side_effect=["y", "y"],  # confirm key, yes persist
            ), patch(
                "bot_bottle.git_gate.host_key.find_and_update_bottle_file",
                return_value=False,
            ), patch("sys.stderr", buf):
                result = preflight_host_keys(
                    manifest, headless=False, home_md=Path(home),
                )

            self.assertEqual("ssh-ed25519 FETCHED", result.bottle.git[0].KnownHostKey)
            self.assertIn("kept in memory", buf.getvalue())

    def test_headless_names_all_missing_repos_in_error(self) -> None:
        manifest = ManifestIndex.from_json_obj({
            "bottles": {
                "dev": {
                    "git-gate": {
                        "repos": {
                            "alpha": {
                                "url": "ssh://git@host/org/alpha.git",
                                "key": {"provider": "static", "path": "/dev/null"},
                            },
                            "beta": {
                                "url": "ssh://git@host/org/beta.git",
                                "key": {"provider": "static", "path": "/dev/null"},
                            },
                        },
                    },
                },
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).load_for_agent("demo")

        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf), self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=True, home_md=None)


if __name__ == "__main__":
    unittest.main()
