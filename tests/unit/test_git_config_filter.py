"""Unit: the repo config a bottle gets (#812).

A copied workspace's `.git/config` is cut to local settings, and remote URLs
lose their credentials; the host's own config never changes. Uses real `git
config`, the same parser the filter uses.
"""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from bot_bottle.git_config_filter import _strip_userinfo, filtered_config, is_repo_config
from bot_bottle.workspace import pack_workspace

SECRETS = ("ghp_SECRET123456789", "c2VjcmV0c2VjcmV0", "tok:abc", ":pw@", "abcdefghijklmnop")


def _git_config(repo: Path, key: str, value: str) -> None:
    subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)


def _read(data: bytes, key: str) -> list[str]:
    """Values of `key` in config bytes `data`, as git reads them."""
    with tempfile.NamedTemporaryFile("wb", delete=False) as f:
        f.write(data)
    try:
        out = subprocess.run(["git", "config", "--file", f.name, "--get-all", key],
                             capture_output=True, check=False).stdout.decode()
    finally:
        Path(f.name).unlink()
    return out.splitlines()


class _Repo(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="git-config-filter.")  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.config = self.repo / ".git" / "config"


class TestFilteredConfig(_Repo):
    def setUp(self) -> None:
        super().setUp()
        for key, value in [
            ("remote.origin.url",
             "https://x-access-token:ghp_SECRET123456789@github.com/acme/app.git"),
            ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ("remote.up.url", "ssh://git:pw@gitea.example.com/acme/app.git"),
            ("remote.mirror.url", "git@github.com:acme/app.git"),
            ("branch.main.remote", "origin"),
            ("branch.main.merge", "refs/heads/main"),
            ("http.https://github.com/.extraHeader", "Authorization: Basic c2VjcmV0c2VjcmV0"),
            ("credential.helper", "osxkeychain"),
            ("url.https://tok:abc@host.example/.insteadOf", "https://host.example/"),
            ("url.https://github.com/.insteadOf", "gh:"),
            ("core.hooksPath", "/Users/me/hooks"),
            ("core.sshCommand", "ssh -i ~/.ssh/id"),
            ("core.autocrlf", "input"),
            ("include.path", "/Users/me/.gitconfig-work"),
            ("includeIf.gitdir:~/work/.path", "/Users/me/.gitconfig-work"),
            ("user.email", "me@example.com"),
            ("user.signingKey", "ABC123"),
            ("alias.st", "status"),
            ("alias.leak", '!curl -H "Authorization: Bearer abcdefghijklmnop" x'),
            ("submodule.lib.url", "https://u:p4ss@github.com/acme/lib.git"),
            ("submodule.lib.update", "!host-script"),
            ("diff.secret.textconv", "/Users/me/bin/decrypt"),
        ]:
            _git_config(self.repo, key, value)
        self.before = self.config.read_bytes()
        self.data, self.dropped = filtered_config(self.config)

    def test_no_secret_reaches_the_bottle(self) -> None:
        for secret in SECRETS:
            self.assertNotIn(secret.encode(), self.data)
        self.assertNotIn(b"p4ss", self.data)

    def test_remote_urls_lose_their_user_info(self) -> None:
        self.assertEqual(_read(self.data, "remote.origin.url"),
                         ["https://github.com/acme/app.git"])
        # A bare ssh user name is not a credential; the password is.
        self.assertEqual(_read(self.data, "remote.up.url"),
                         ["ssh://git@gitea.example.com/acme/app.git"])
        self.assertEqual(_read(self.data, "remote.mirror.url"), ["git@github.com:acme/app.git"])
        self.assertEqual(_read(self.data, "submodule.lib.url"),
                         ["https://github.com/acme/lib.git"])

    def test_local_settings_are_kept(self) -> None:
        self.assertEqual(_read(self.data, "remote.origin.fetch"),
                         ["+refs/heads/*:refs/remotes/origin/*"])
        self.assertEqual(_read(self.data, "branch.main.merge"), ["refs/heads/main"])
        self.assertEqual(_read(self.data, "core.autocrlf"), ["input"])
        self.assertEqual(_read(self.data, "core.repositoryformatversion"), ["0"])
        self.assertEqual(_read(self.data, "user.email"), ["me@example.com"])
        self.assertEqual(_read(self.data, "alias.st"), ["status"])
        # A plain rewrite carries no credential.
        self.assertEqual(_read(self.data, "url.https://github.com/.insteadof"), ["gh:"])

    def test_host_paths_commands_and_includes_stay_behind(self) -> None:
        for key in ("http.https://github.com/.extraheader", "credential.helper",
                    "core.hookspath", "core.sshcommand", "include.path",
                    "includeif.gitdir:~/work/.path", "user.signingkey", "alias.leak",
                    "submodule.lib.update", "diff.secret.textconv"):
            self.assertEqual(_read(self.data, key), [], key)

    def test_left_out_credentials_are_named_never_shown(self) -> None:
        self.assertIn("remote.origin.url user info", self.dropped)
        self.assertIn("http.https://github.com/.extraheader", self.dropped)
        self.assertIn("url.<url with credentials>.insteadof", self.dropped)
        self.assertNotIn("core.hookspath", self.dropped)  # not a credential
        for secret in SECRETS:
            self.assertFalse(any(secret in label for label in self.dropped), secret)

    def test_the_host_config_is_untouched(self) -> None:
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_a_clean_config_drops_nothing(self) -> None:
        clean = self.repo.parent / "clean"
        subprocess.run(["git", "init", "-q", str(clean)], check=True)
        _git_config(clean, "remote.origin.url", "https://github.com/acme/app.git")
        _data, dropped = filtered_config(clean / ".git" / "config")
        self.assertEqual(dropped, [])

    def test_an_unreadable_config_gives_an_empty_one(self) -> None:
        bad = self.repo.parent / "bad-config"
        bad.write_text("[core\n\tbroken")
        data, dropped = filtered_config(bad)
        self.assertEqual(data, b"")
        self.assertEqual(dropped, ["(unreadable repo config)"])


class TestHelpers(unittest.TestCase):
    def test_strip_userinfo(self) -> None:
        cases = {
            "https://tok@github.com/a/b": "https://github.com/a/b",
            "https://u:p@github.com/a/b": "https://github.com/a/b",
            "ssh://git@host/a/b": "ssh://git@host/a/b",
            "ssh://git:pw@host/a/b": "ssh://git@host/a/b",
            "git@github.com:a/b.git": "git@github.com:a/b.git",
            "https://github.com/a/b": "https://github.com/a/b",
            "/srv/repos/a.git": "/srv/repos/a.git",
        }
        for url, want in cases.items():
            self.assertEqual(_strip_userinfo(url), want, url)

    def test_is_repo_config(self) -> None:
        for rel in (".git/config", ".git/modules/lib/config", ".git/worktrees/wt/config.worktree"):
            self.assertTrue(is_repo_config(rel), rel)
        for rel in ("config", "src/.git/config", ".git/hooks/config", ".git/info/exclude"):
            self.assertFalse(is_repo_config(rel), rel)


class TestPackWorkspace(_Repo):
    def test_the_packed_config_is_the_filtered_one(self) -> None:
        _git_config(self.repo, "remote.origin.url", "https://tok123@github.com/acme/app.git")
        (self.repo / "app.py").write_text("print(1)\n")
        tar_path = self.repo.parent / "ws.tar"
        dropped: list[str] = []
        pack_workspace(self.repo, tar_path, is_git=True, dropped=dropped)
        with tarfile.open(tar_path) as tar:
            member = tar.getmember(".git/config")
            extracted = tar.extractfile(member)
            assert extracted is not None
            packed = extracted.read()
            self.assertIn("app.py", tar.getnames())
        self.assertNotIn(b"tok123", packed)
        self.assertEqual(member.size, len(packed))
        self.assertEqual(_read(packed, "remote.origin.url"), ["https://github.com/acme/app.git"])
        self.assertEqual(dropped, ["remote.origin.url user info"])
        self.assertIn(b"tok123", self.config.read_bytes())

    def test_a_submodule_config_is_filtered_too(self) -> None:
        sub = self.repo / ".git" / "modules" / "lib"
        sub.mkdir(parents=True)
        (sub / "config").write_text('[remote "origin"]\n\turl = https://t0k@github.com/a/lib\n')
        dropped: list[str] = []
        pack_workspace(self.repo, self.repo.parent / "ws.tar", is_git=True, dropped=dropped)
        self.assertEqual(dropped, [".git/modules/lib/config: remote.origin.url user info"])

    def test_a_non_git_workspace_is_copied_as_is(self) -> None:
        plain = self.repo.parent / "plain"
        plain.mkdir()
        (plain / "config").write_text("token = https://t0k@host\n")
        tar_path = self.repo.parent / "plain.tar"
        pack_workspace(plain, tar_path, is_git=False)
        with tarfile.open(tar_path) as tar:
            extracted = tar.extractfile("config")
            assert extracted is not None
            self.assertIn(b"t0k", extracted.read())


if __name__ == "__main__":
    unittest.main()
