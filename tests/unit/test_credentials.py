"""Unit tests for bot_bottle.credentials."""

import configparser
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from bot_bottle.credentials import (
    KIND_TOKEN, KIND_AWS, KIND_SSH_KEY,
    KNOWN_KINDS, ENV_KINDS, AUTH_BEARER_KINDS, AUTH_AWS_KINDS, GIT_KEY_KINDS,
    Grant, MintedCredential, CredentialProvider, CredentialRegistry,
    _SecretProvider, _PromptProvider, _FileProvider, _ProfileProvider, default_registry,
)
from bot_bottle.secret_source import MappingSecrets


class TestKindConstants(unittest.TestCase):
    def test_known_kinds_complete(self):
        self.assertEqual(KNOWN_KINDS, {KIND_TOKEN, KIND_AWS, KIND_SSH_KEY})

    def test_set_partitions(self):
        self.assertIn(KIND_TOKEN, ENV_KINDS)
        self.assertNotIn(KIND_AWS, ENV_KINDS)
        self.assertIn(KIND_TOKEN, AUTH_BEARER_KINDS)
        self.assertIn(KIND_AWS, AUTH_AWS_KINDS)
        self.assertIn(KIND_SSH_KEY, GIT_KEY_KINDS)


class TestMintedCredentialRepr(unittest.TestCase):
    def test_repr_hides_values(self):
        cred = MintedCredential(KIND_TOKEN, {"token": "super-secret"}, None, "me")
        r = repr(cred)
        self.assertNotIn("super-secret", r)
        self.assertIn(KIND_TOKEN, r)
        self.assertIn("me", r)

    def test_expires_at_in_repr(self):
        ts = datetime(2026, 1, 1)
        cred = MintedCredential(KIND_TOKEN, {"token": "x"}, ts, "who")
        self.assertIn("2026", repr(cred))


class TestSecretProvider(unittest.TestCase):
    def setUp(self):
        self.provider = _SecretProvider()
        self.secrets = MappingSecrets({"MY_TOKEN": "tok123"})

    def test_mint_token(self):
        cred = self.provider.mint("tok", "bottle1",
                                  {"secret": "MY_TOKEN"}, self.secrets)
        self.assertEqual(cred.kind, KIND_TOKEN)
        self.assertEqual(cred.values["token"], "tok123")
        self.assertIsNone(cred.expires_at)

    def test_mint_missing_secret_key_raises(self):
        with self.assertRaises(ValueError, msg="secret: NAME required"):
            self.provider.mint("tok", "bottle1", {}, self.secrets)

    def test_mint_aws(self):
        s = MappingSecrets({"AKI": "AKID", "SAK": "sak-val", "ST": "token-val"})
        cred = self.provider.mint("aws", "bottle1",
                                  {"kind": KIND_AWS,
                                   "access_key_id": "AKI",
                                   "secret_access_key": "SAK",
                                   "session_token": "ST"}, s)
        self.assertEqual(cred.kind, KIND_AWS)
        self.assertEqual(cred.values["access_key_id"], "AKID")
        self.assertIn("session_token", cred.values)

    def test_mint_aws_missing_key_raises(self):
        s = MappingSecrets({"AKI": "AKID"})
        with self.assertRaises(ValueError):
            self.provider.mint("aws", "b", {"kind": KIND_AWS,
                                            "access_key_id": "AKI"}, s)

    def test_mint_ssh_key(self):
        s = MappingSecrets({"PRIV": "priv-pem", "PUB": "pub-pem"})
        cred = self.provider.mint("key", "b",
                                  {"kind": KIND_SSH_KEY,
                                   "private_key": "PRIV",
                                   "public_key": "PUB"}, s)
        self.assertEqual(cred.kind, KIND_SSH_KEY)
        self.assertEqual(cred.values["private_key"], "priv-pem")

    def test_renew_calls_mint(self):
        old = self.provider.mint("tok", "b", {"secret": "MY_TOKEN"}, self.secrets)
        new = self.provider.renew("tok", "b", old, {"secret": "MY_TOKEN"}, self.secrets)
        self.assertEqual(new.values["token"], old.values["token"])

    def test_describe_token(self):
        g = self.provider.describe({"secret": "MY_TOKEN"})
        self.assertIsInstance(g, Grant)

    def test_project_definable(self):
        self.assertTrue(self.provider.project_definable)

    def test_source_name(self):
        self.assertEqual(self.provider.source, "secret")

    def test_kinds(self):
        self.assertIn(KIND_TOKEN, self.provider.kinds)
        self.assertIn(KIND_AWS, self.provider.kinds)
        self.assertIn(KIND_SSH_KEY, self.provider.kinds)


class TestPromptProvider(unittest.TestCase):
    def setUp(self):
        self.provider = _PromptProvider()

    def test_mint_reads_prompt_key(self):
        s = MappingSecrets({"__prompt__my-cred": "user-answer"})
        cred = self.provider.mint("my-cred", "b", {"message": "Enter token"}, s)
        self.assertEqual(cred.kind, KIND_TOKEN)
        self.assertEqual(cred.values["token"], "user-answer")

    def test_mint_absent_answer_is_empty(self):
        s = MappingSecrets({})
        cred = self.provider.mint("tok", "b", {}, s)
        self.assertEqual(cred.values["token"], "")

    def test_describe(self):
        g = self.provider.describe({"message": "Enter your API key"})
        self.assertIn("Enter your API key", g.starts_from)

    def test_source_name(self):
        self.assertEqual(self.provider.source, "prompt")

    def test_kinds(self):
        self.assertEqual(self.provider.kinds, (KIND_TOKEN,))


class TestFileProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.provider = _FileProvider()
        self.secrets = MappingSecrets({})

    def _write(self, name: str, content: str) -> str:
        p = self.tmp / name
        p.write_text(content)
        return str(p)

    def test_mint_token_reads_file(self) -> None:
        path = self._write("tok.txt", "my-api-key\n")
        cred = self.provider.mint("tok", "b", {"kind": "token", "path": path}, self.secrets)
        self.assertEqual(KIND_TOKEN, cred.kind)
        self.assertEqual("my-api-key", cred.values["token"])

    def test_mint_token_strips_whitespace(self) -> None:
        path = self._write("tok.txt", "  the-token  \n")
        cred = self.provider.mint("tok", "b", {"kind": "token", "path": path}, self.secrets)
        self.assertEqual("the-token", cred.values["token"])

    def test_mint_token_missing_path_raises(self) -> None:
        with self.assertRaises(ValueError, msg="path required"):
            self.provider.mint("tok", "b", {"kind": "token"}, self.secrets)

    def test_mint_ssh_key_default_kind(self) -> None:
        priv = self._write("id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n")
        pub = self._write("id_ed25519.pub", "ssh-ed25519 AAAA... comment\n")
        cred = self.provider.mint("key", "b",
                                  {"private_key_path": priv, "public_key_path": pub},
                                  self.secrets)
        self.assertEqual(KIND_SSH_KEY, cred.kind)
        self.assertIn("PRIVATE KEY", cred.values["private_key"])
        self.assertEqual("ssh-ed25519 AAAA... comment", cred.values["public_key"])

    def test_mint_ssh_key_missing_path_raises(self) -> None:
        priv = self._write("id_ed25519", "priv")
        with self.assertRaises(ValueError, msg="public_key_path required"):
            self.provider.mint("key", "b", {"private_key_path": priv}, self.secrets)

    def test_describe_token(self) -> None:
        g = self.provider.describe({"kind": "token", "path": "/run/secrets/api-key"})
        self.assertIn("/run/secrets/api-key", g.starts_from)

    def test_describe_ssh_key(self) -> None:
        g = self.provider.describe({"private_key_path": "/home/user/.ssh/id_ed25519"})
        self.assertIn("/home/user/.ssh/id_ed25519", g.starts_from)

    def test_source_name(self) -> None:
        self.assertEqual("file", self.provider.source)

    def test_not_project_definable(self) -> None:
        self.assertFalse(self.provider.project_definable)

    def test_kinds(self) -> None:
        self.assertIn(KIND_SSH_KEY, self.provider.kinds)
        self.assertIn(KIND_TOKEN, self.provider.kinds)


class TestProfileProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.creds_file = self.tmp / "credentials"
        self.provider = _ProfileProvider()
        self.secrets = MappingSecrets({})

    def _write_creds(self, content: str) -> None:
        self.creds_file.write_text(content)

    def _mint(self, settings: dict) -> MintedCredential:
        env = {"AWS_SHARED_CREDENTIALS_FILE": str(self.creds_file)}
        with unittest.mock.patch.dict(os.environ, env):
            return self.provider.mint("creds", "b", settings, self.secrets)

    def test_mint_reads_default_profile(self) -> None:
        self._write_creds("[default]\naws_access_key_id = AKIA\naws_secret_access_key = SAK\n")
        cred = self._mint({})
        self.assertEqual(KIND_AWS, cred.kind)
        self.assertEqual("AKIA", cred.values["access_key_id"])
        self.assertEqual("SAK", cred.values["secret_access_key"])
        self.assertNotIn("session_token", cred.values)

    def test_mint_reads_named_profile(self) -> None:
        self._write_creds(
            "[default]\naws_access_key_id = DEF\naws_secret_access_key = DEF_S\n"
            "[staging]\naws_access_key_id = STAGE\naws_secret_access_key = STAGE_S\n"
        )
        cred = self._mint({"profile_name": "staging"})
        self.assertEqual("STAGE", cred.values["access_key_id"])

    def test_mint_includes_session_token_when_present(self) -> None:
        self._write_creds(
            "[default]\naws_access_key_id = X\naws_secret_access_key = Y\n"
            "aws_session_token = TOK\n"
        )
        cred = self._mint({})
        self.assertEqual("TOK", cred.values["session_token"])

    def test_mint_missing_credentials_file_raises(self) -> None:
        with unittest.mock.patch.dict(os.environ,
                                      {"AWS_SHARED_CREDENTIALS_FILE": "/no/such/file"}):
            with self.assertRaises(ValueError, msg="not found"):
                self.provider.mint("creds", "b", {}, self.secrets)

    def test_mint_missing_profile_raises(self) -> None:
        self._write_creds("[default]\naws_access_key_id = X\naws_secret_access_key = Y\n")
        with self.assertRaises(ValueError, msg="not found"):
            self._mint({"profile_name": "nonexistent"})

    def test_mint_missing_access_key_raises(self) -> None:
        self._write_creds("[default]\naws_access_key_id = AKIA\n")
        with self.assertRaises(ValueError, msg="missing"):
            self._mint({})

    def test_describe(self) -> None:
        g = self.provider.describe({"profile_name": "prod"})
        self.assertIn("prod", g.starts_from)

    def test_describe_default_profile(self) -> None:
        g = self.provider.describe({})
        self.assertIn("default", g.starts_from)

    def test_source_name(self) -> None:
        self.assertEqual("profile", self.provider.source)

    def test_not_project_definable(self) -> None:
        self.assertFalse(self.provider.project_definable)


class TestCredentialRegistry(unittest.TestCase):
    def test_built_ins_present(self):
        reg = CredentialRegistry(contrib_root=None)
        self.assertIsNotNone(reg.get("secret"))
        self.assertIsNotNone(reg.get("prompt"))

    def test_unknown_source_returns_none(self):
        reg = CredentialRegistry(contrib_root=None)
        self.assertIsNone(reg.get("does-not-exist"))

    def test_known_sources(self):
        reg = CredentialRegistry(contrib_root=None)
        self.assertIn("secret", reg.known_sources())
        self.assertIn("prompt", reg.known_sources())

    def test_default_registry_singleton(self):
        import bot_bottle.credentials as mod
        mod._default_registry = None  # reset between tests
        r1 = default_registry()
        r2 = default_registry()
        self.assertIs(r1, r2)
        mod._default_registry = None


if __name__ == "__main__":
    unittest.main()
