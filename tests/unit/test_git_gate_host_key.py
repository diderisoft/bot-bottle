"""Unit tests for git_gate_host_key (issue #333)."""

from __future__ import annotations

import dataclasses
import unittest
from unittest.mock import MagicMock, patch

from bot_bottle.git_gate.host_key import (
    prompt_tty,
    fetch_host_key,
    preflight_host_keys,
)
from bot_bottle.manifest import Manifest, ManifestIndex
from bot_bottle.manifest.git import parse_git_gate_config


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

    def test_raises_with_repeated_stderr_once(self) -> None:
        r = self._run_result("")
        r.stderr = "write (10.0.0.1): Broken pipe\nwrite (10.0.0.1): Broken pipe\n"
        with patch("subprocess.run", return_value=r):
            with self.assertRaises(RuntimeError) as cm:
                fetch_host_key("10.0.0.1", "30009")
        self.assertEqual("ssh-keyscan got no host key from 10.0.0.1:30009 "
                         "(write (10.0.0.1): Broken pipe)", str(cm.exception))

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
# preflight_host_keys
# ---------------------------------------------------------------------------


def _manifest_with_git(host_key: str = "", names: tuple[str, ...] = ("myrepo",)) -> Manifest:
    """A Manifest whose bottle has git entries (as grants expand to; a forge
    connected without a pin leaves `host_key` empty)."""
    manifest = ManifestIndex.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {"demo": {"skills": [], "prompt": ""}},
    }).load_for_agent("demo", ("dev",))
    git, _ = parse_git_gate_config("dev", {"repos": {
        name: {"url": f"ssh://git@gitea.example.com:30009/org/{name}.git",
               "key": {"provider": "ssh", "path": "/dev/null"},
               **({"host_key": host_key} if host_key else {})}
        for name in names}})
    return dataclasses.replace(manifest, bottle=dataclasses.replace(manifest.bottle, git=git))


class TestPreflightHostKeys(unittest.TestCase):
    def test_no_op_when_all_keys_present(self) -> None:
        manifest = _manifest_with_git("ssh-ed25519 AAAA")
        result = preflight_host_keys(manifest, headless=False)
        self.assertIs(result, manifest)

    def test_headless_dies_when_key_missing(self) -> None:
        manifest = _manifest_with_git()
        with self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=True)

    def test_interactive_fetches_and_confirms_key(self) -> None:
        manifest = _manifest_with_git()

        with patch(
            "bot_bottle.git_gate.host_key.fetch_host_key",
            return_value="ssh-ed25519 FETCHED",
        ), patch(
            "bot_bottle.git_gate.host_key.prompt_tty",
            side_effect=["y"],  # confirm key
        ), patch("sys.stderr"):
            result = preflight_host_keys(manifest, headless=False)

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
            preflight_host_keys(manifest, headless=False)

    def test_interactive_dies_when_fetch_fails(self) -> None:
        manifest = _manifest_with_git()

        with patch(
            "bot_bottle.git_gate.host_key.fetch_host_key",
            side_effect=RuntimeError("connection refused"),
        ), patch("sys.stderr"), self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=False)

    def test_headless_names_all_missing_repos_in_error(self) -> None:
        manifest = _manifest_with_git(names=("alpha", "beta"))
        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf), self.assertRaises(SystemExit):
            preflight_host_keys(manifest, headless=True)
        self.assertIn("'alpha', 'beta'", buf.getvalue())
        self.assertIn("bot-bottle forge connect", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
