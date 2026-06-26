"""Unit: git_gate gitconfig rendering + deploy-key provision/revoke
(coverage ratchet, ADR 0004).

Covers the pure `git_gate_render_gitconfig` renderer and the dynamic
(gitea) deploy-key lifecycle, with the forge provisioner mocked."""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from bot_bottle.git_gate import (
    _gitconfig_validate_value,
    _provision_dynamic_key,
    git_gate_render_gitconfig,
    revoke_git_gate_provisioned_keys,
)
from bot_bottle.manifest_git import ManifestGitEntry, ManifestKeyConfig


def _entry(**kw: Any) -> ManifestGitEntry:
    base: dict[str, Any] = {
        "Name": "repo",
        "Upstream": "git@github.com:o/r.git",
        "UpstreamHost": "github.com",
        "UpstreamUser": "git",
        "UpstreamPath": "o/r.git",
        "UpstreamPort": "22",
    }
    base.update(kw)
    return ManifestGitEntry(**base)


def _gitea_entry(**kw: Any) -> ManifestGitEntry:
    return _entry(
        Key=ManifestKeyConfig(provider="gitea", forge_token_env="GITEA_TOK"),
        **kw,
    )


class _FakeProvisioner:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def create(self, owner_repo: str, title: str) -> tuple[str, bytes]:
        self.created.append((owner_repo, title))
        return "kid123", b"PRIVATE-KEY-BYTES"

    def delete(self, owner_repo: str, key_id: str) -> None:
        self.deleted.append((owner_repo, key_id))


# ---------------------------------------------------------------------------
# git_gate_render_gitconfig
# ---------------------------------------------------------------------------


class TestRenderGitconfig(unittest.TestCase):
    def test_empty_entries_returns_empty_string(self) -> None:
        self.assertEqual("", git_gate_render_gitconfig((), "git-gate"))

    def test_single_entry_renders_insteadof(self) -> None:
        out = git_gate_render_gitconfig((_entry(),), "git-gate")
        self.assertIn('[url "git://git-gate/repo.git"]', out)
        self.assertIn("insteadOf = git@github.com:o/r.git", out)

    def test_scheme_override(self) -> None:
        out = git_gate_render_gitconfig((_entry(),), "1.2.3.4:9418", scheme="http")
        self.assertIn('[url "http://1.2.3.4:9418/repo.git"]', out)

    def test_remote_key_alias_with_nondefault_port(self) -> None:
        out = git_gate_render_gitconfig(
            (_entry(RemoteKey="10.0.0.5", UpstreamPort="2222"),), "git-gate",
        )
        self.assertIn("insteadOf = ssh://git@10.0.0.5:2222/o/r.git", out)

    def test_remote_key_alias_default_port_omits_port(self) -> None:
        out = git_gate_render_gitconfig(
            (_entry(RemoteKey="10.0.0.5", UpstreamPort="22"),), "git-gate",
        )
        self.assertIn("insteadOf = ssh://git@10.0.0.5/o/r.git", out)
        self.assertNotIn(":22/", out)

    def test_validate_rejects_newline(self) -> None:
        with self.assertRaises(ValueError):
            _gitconfig_validate_value("field", "line1\nline2")

    def test_render_rejects_newline_in_upstream(self) -> None:
        with self.assertRaises(ValueError):
            git_gate_render_gitconfig((_entry(Upstream="a\nb"),), "git-gate")


# ---------------------------------------------------------------------------
# _provision_dynamic_key
# ---------------------------------------------------------------------------


class TestProvisionDynamicKey(unittest.TestCase):
    def test_happy_path_writes_key_and_id(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch.dict("os.environ", {"GITEA_TOK": "secret-token"}), \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch("sys.stderr"):
            path = _provision_dynamic_key(_gitea_entry(), "myslug", Path(d))
            key_file = Path(path)
            self.assertEqual(b"PRIVATE-KEY-BYTES", key_file.read_bytes())
            id_file = Path(d) / "repo-deploy-key-id"
            self.assertEqual("kid123", id_file.read_text())
        # owner_repo had .git stripped; title carries slug + name
        self.assertEqual([("o/r", "bot-bottle:myslug:repo")], fake.created)

    def test_missing_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GITEA_TOK", None)
            with self.assertRaises(RuntimeError):
                _provision_dynamic_key(_gitea_entry(), "s", Path(d))


# ---------------------------------------------------------------------------
# revoke_git_gate_provisioned_keys
# ---------------------------------------------------------------------------


def _bottle(*entries: ManifestGitEntry) -> Any:
    return cast(Any, types.SimpleNamespace(git=entries))


class TestRevokeProvisionedKeys(unittest.TestCase):
    def test_revokes_gitea_key_when_id_present(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch.dict("os.environ", {"GITEA_TOK": "secret-token"}), \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch("sys.stderr"):
            (Path(d) / "repo-deploy-key-id").write_text("kid123")
            revoke_git_gate_provisioned_keys(_bottle(_gitea_entry()), Path(d))
        self.assertEqual([("o/r", "kid123")], fake.deleted)

    def test_skips_non_gitea_entry(self) -> None:
        fake = _FakeProvisioner()
        static_entry = _entry(Key=ManifestKeyConfig(provider="static", path="/k"))
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake):
            revoke_git_gate_provisioned_keys(_bottle(static_entry), Path(d))
        self.assertEqual([], fake.deleted)

    def test_skips_when_id_file_missing(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake):
            # no id file written -> entry skipped
            revoke_git_gate_provisioned_keys(_bottle(_gitea_entry()), Path(d))
        self.assertEqual([], fake.deleted)

    def test_missing_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GITEA_TOK", None)
            (Path(d) / "repo-deploy-key-id").write_text("kid123")
            with self.assertRaises(RuntimeError):
                revoke_git_gate_provisioned_keys(_bottle(_gitea_entry()), Path(d))


if __name__ == "__main__":
    unittest.main()
