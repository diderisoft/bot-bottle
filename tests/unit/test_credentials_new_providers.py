"""Tests for the credential providers beyond chunk 1's: file and
gitea_deploy_key."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.credentials import (
    KIND_SSH_KEY,
    KIND_TOKEN,
    MintedCredential,
    _FileProvider,
    _GiteaDeployKeyProvider,
)
from bot_bottle.secret_source import MappingSecrets

_SECRETS = MappingSecrets({})


# ── _FileProvider ─────────────────────────────────────────────────────────────

class TestFileProvider(unittest.TestCase):

    def setUp(self) -> None:
        self.provider = _FileProvider()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _write(self, name: str, content: str) -> Path:
        p = Path(self.tmp) / name
        p.write_text(content)
        return p

    def test_mint_with_pub_file(self) -> None:
        priv = self._write("id_ed25519", "PRIVATE")
        self._write("id_ed25519.pub", "PUBLIC")
        cred = self.provider.mint("key", "b1", {"private_key_path": str(priv)}, _SECRETS)
        self.assertEqual(cred.kind, KIND_SSH_KEY)
        self.assertEqual(cred.values["private_key"], "PRIVATE")
        self.assertEqual(cred.values["public_key"], "PUBLIC")
        self.assertIsNone(cred.expires_at)

    def test_mint_without_pub_file(self) -> None:
        priv = self._write("id_no_pub", "PRIVATEONLY")
        cred = self.provider.mint("key", "b1", {"private_key_path": str(priv)}, _SECRETS)
        self.assertEqual(cred.values["private_key"], "PRIVATEONLY")
        self.assertEqual(cred.values["public_key"], "")

    def test_mint_missing_private_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.mint("key", "b1", {}, _SECRETS)

    def test_mint_nonexistent_file_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.mint("key", "b1",
                               {"private_key_path": "/nonexistent/path/key"}, _SECRETS)

    def test_mint_nonexistent_public_key_path_raises(self) -> None:
        priv = self._write("id_ed25519", "PRIVATE")
        with self.assertRaises(ValueError):
            self.provider.mint("key", "b1", {"private_key_path": str(priv),
                                             "public_key_path": "/nonexistent.pub"}, _SECRETS)

    def test_renew_re_reads_file(self) -> None:
        priv = self._write("id_ed25519", "PRIVATE_V1")
        settings = {"private_key_path": str(priv)}
        old = self.provider.mint("key", "b1", settings, _SECRETS)
        priv.write_text("PRIVATE_V2")
        fresh = self.provider.renew("key", "b1", old, settings, _SECRETS)
        self.assertEqual(fresh.values["private_key"], "PRIVATE_V2")

    def test_token_kind_reads_path(self) -> None:
        tok = self._write("tok.txt", "  the-token  \n")
        cred = self.provider.mint("t", "b1", {"kind": KIND_TOKEN, "path": str(tok)}, _SECRETS)
        self.assertEqual(cred.kind, KIND_TOKEN)
        self.assertEqual(cred.values["token"], "the-token")

    def test_token_kind_missing_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.mint("t", "b1", {"kind": KIND_TOKEN}, _SECRETS)

    def test_explicit_public_key_path(self) -> None:
        priv = self._write("k", "PRIV")
        pub = self._write("elsewhere.pub", "ssh-ed25519 AAAA comment\n")
        cred = self.provider.mint("key", "b1", {"private_key_path": str(priv),
                                                "public_key_path": str(pub)}, _SECRETS)
        self.assertEqual(cred.values["public_key"], "ssh-ed25519 AAAA comment")

    def test_describe_ssh_key(self) -> None:
        g = self.provider.describe({"private_key_path": "/home/user/.ssh/id_ed25519"})
        self.assertIn("/home/user/.ssh/id_ed25519", g.starts_from)

    def test_describe_token(self) -> None:
        g = self.provider.describe({"kind": KIND_TOKEN, "path": "/run/secrets/api-key"})
        self.assertIn("/run/secrets/api-key", g.starts_from)

    def test_source_kinds_and_scope(self) -> None:
        self.assertEqual(self.provider.source, "file")
        self.assertEqual(self.provider.kinds, (KIND_SSH_KEY, KIND_TOKEN))
        self.assertFalse(self.provider.project_definable)

    def test_file_fields(self) -> None:
        self.assertIn("private_key_path", self.provider.file_fields)
        self.assertIn("path", self.provider.file_fields)

    def test_tilde_expansion(self) -> None:
        """`~` in a path is expanded."""
        priv = self._write("id_tilde", "TILDE_KEY")
        with mock.patch("pathlib.Path.expanduser", return_value=priv):
            cred = self.provider.mint("key", "b1", {"private_key_path": "~/id_tilde"},
                                      _SECRETS)
        self.assertEqual(cred.values["private_key"], "TILDE_KEY")


# ── _GiteaDeployKeyProvider ───────────────────────────────────────────────────

class TestGiteaDeployKeyProvider(unittest.TestCase):

    def setUp(self) -> None:
        self.provider = _GiteaDeployKeyProvider()
        self._settings: dict[str, object] = {
            "api_url": "https://gitea.example.com",
            "token": "gitea-pat-xyz",
            "repo": "owner/repo",
        }

    def _make_mock_prov(self, key_id: str = "42") -> mock.MagicMock:
        prov = mock.MagicMock()
        prov.create.return_value = (key_id, b"PRIV-KEY-BYTES")
        return prov

    def test_mint_calls_provisioner(self) -> None:
        mock_prov = self._make_mock_prov("99")
        with mock.patch("bot_bottle.deploy_key_provisioner.get_provisioner",
                        return_value=mock_prov):
            cred = self.provider.mint("dk", "bottle1", self._settings, _SECRETS)

        self.assertEqual(cred.kind, KIND_SSH_KEY)
        self.assertEqual(cred.values["private_key"], "PRIV-KEY-BYTES")
        self.assertEqual(cred.retire_data["key_id"], "99")
        self.assertEqual(cred.retire_data["repo"], "owner/repo")
        mock_prov.create.assert_called_once_with(
            "owner/repo", "bot-bottle:bottle1:dk", read_only=True)

    def test_retire_calls_delete(self) -> None:
        mock_prov = mock.MagicMock()
        cred = MintedCredential(
            KIND_SSH_KEY, {"private_key": "P", "public_key": "Q"}, None,
            "gitea_deploy_key:owner/repo",
            retire_data={
                "key_id": "77", "api_url": "https://g.example.com",
                "token": "tok", "repo": "owner/repo",
            },
        )
        with mock.patch("bot_bottle.deploy_key_provisioner.get_provisioner",
                        return_value=mock_prov):
            self.provider.retire("dk", "bottle1", cred)
        mock_prov.delete.assert_called_once_with("owner/repo", "77")

    def test_retire_missing_retire_data_is_noop(self) -> None:
        cred = MintedCredential(KIND_SSH_KEY, {"private_key": "P", "public_key": "Q"},
                                None, "gitea_deploy_key:x")
        self.provider.retire("dk", "b", cred)  # doesn't raise

    def test_missing_settings_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.mint("dk", "b", {"token": "t", "repo": "r"}, _SECRETS)

    def test_project_definable(self) -> None:
        self.assertTrue(self.provider.project_definable)

    def test_describe(self) -> None:
        g = self.provider.describe(self._settings)
        self.assertIn("owner/repo", g.acts_as)
        self.assertIn("teardown", g.lifetime)


if __name__ == "__main__":
    unittest.main()
