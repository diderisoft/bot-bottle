"""Unit tests for bot_bottle.credentials."""

import email.message
import os
import unittest
import unittest.mock
from datetime import datetime
from typing import Any

from bot_bottle.credentials import (
    KIND_TOKEN, KIND_AWS, KIND_SSH_KEY,
    KNOWN_KINDS, ENV_KINDS, AUTH_BEARER_KINDS, AUTH_AWS_KINDS, GIT_KEY_KINDS,
    Grant, MintedCredential, CredentialRegistry,
    _AssumeRoleProvider, _SecretProvider, _PromptProvider,
    _sts_assume_role, default_registry,
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


class TestCredentialRegistry(unittest.TestCase):
    def test_built_ins_present(self):
        reg = CredentialRegistry(contrib_root=None)
        self.assertIsNotNone(reg.get("secret"))
        self.assertIsNotNone(reg.get("prompt"))
        self.assertIsNotNone(reg.get("assume_role"))

    def test_unknown_source_returns_none(self):
        reg = CredentialRegistry(contrib_root=None)
        self.assertIsNone(reg.get("does-not-exist"))

    def test_known_sources(self):
        reg = CredentialRegistry(contrib_root=None)
        self.assertIn("secret", reg.known_sources())
        self.assertIn("prompt", reg.known_sources())
        self.assertIn("assume_role", reg.known_sources())

    def test_default_registry_singleton(self):
        import bot_bottle.credentials as mod
        mod._default_registry = None  # reset between tests
        r1 = default_registry()
        r2 = default_registry()
        self.assertIs(r1, r2)
        mod._default_registry = None


_STS_XML = """\
<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleResult>
    <Credentials>
      <AccessKeyId>ASIA_NEW</AccessKeyId>
      <SecretAccessKey>new_secret</SecretAccessKey>
      <SessionToken>new_token</SessionToken>
      <Expiration>2026-01-01T12:00:00Z</Expiration>
    </Credentials>
  </AssumeRoleResult>
</AssumeRoleResponse>"""

_STS_ERROR_XML = """\
<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <Error>
    <Code>AccessDenied</Code>
    <Message>User is not authorized to assume role</Message>
  </Error>
</ErrorResponse>"""


class TestStsAssumeRole(unittest.TestCase):
    """Unit tests for the stdlib STS helper (network stubbed)."""

    def _call(self, xml_response: str, status: int = 200) -> dict[str, object]:
        import io
        import unittest.mock as mock
        import urllib.error

        if status == 200:
            resp = mock.MagicMock()
            resp.read.return_value = xml_response.encode()
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            with mock.patch("urllib.request.urlopen", return_value=resp):
                return _sts_assume_role(
                    "AKIA", "secret", None,
                    role_arn="arn:aws:iam::123:role/my-role",
                    session_name="my_sess",
                )
        else:
            err = urllib.error.HTTPError(
                "https://sts.amazonaws.com/", status, "Error", email.message.Message(),
                io.BytesIO(xml_response.encode()))
            with mock.patch("urllib.request.urlopen", side_effect=err):
                return _sts_assume_role(
                    "AKIA", "secret", None,
                    role_arn="arn:aws:iam::123:role/my-role",
                    session_name="my_sess",
                )

    def test_happy_path_returns_credentials(self) -> None:
        result = self._call(_STS_XML)
        self.assertEqual("ASIA_NEW", result["access_key_id"])
        self.assertEqual("new_secret", result["secret_access_key"])
        self.assertEqual("new_token", result["session_token"])
        self.assertIsNotNone(result["expires_at"])

    def test_expiry_is_parsed_as_datetime(self) -> None:
        from datetime import datetime, timezone
        result = self._call(_STS_XML)
        self.assertIsInstance(result["expires_at"], datetime)
        self.assertEqual(timezone.utc, result["expires_at"].tzinfo)  # type: ignore[union-attr]

    def test_http_error_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "AccessDenied"):
            self._call(_STS_ERROR_XML, status=403)


class TestAssumeRoleProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _AssumeRoleProvider()
        self.secrets = MappingSecrets({})
        self.base_cred = MintedCredential(
            KIND_AWS,
            {"access_key_id": "BASE_KEY", "secret_access_key": "BASE_SECRET"},
            None, "base",
        )

    def _mint(self, settings: dict[str, object],
              minted_creds: dict[str, MintedCredential] | None = None,
              sts_xml: str = _STS_XML) -> MintedCredential:
        import unittest.mock as mock
        resp = mock.MagicMock()
        resp.read.return_value = sts_xml.encode()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        env = {"AWS_ACCESS_KEY_ID": "ENV_KEY", "AWS_SECRET_ACCESS_KEY": "ENV_SECRET"}
        with mock.patch("urllib.request.urlopen", return_value=resp), \
                unittest.mock.patch.dict(os.environ, env):
            return self.provider.mint("my-role", "b", settings, self.secrets,
                                      minted_creds=minted_creds)

    def test_mint_with_base_cred_uses_it(self) -> None:
        import unittest.mock as mock
        captured: list[Any] = []

        def _fake_urlopen(req: Any, timeout: float | None = None) -> Any:
            del timeout
            captured.append(req)
            resp = mock.MagicMock()
            resp.read.return_value = _STS_XML.encode()
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            self.provider.mint(
                "my-role", "b",
                {"role_arn": "arn:aws:iam::123:role/my-role", "base": "@base-aws"},
                self.secrets,
                minted_creds={"base-aws": self.base_cred},
            )
        req = captured[0]
        auth = req.get_header("Authorization")
        self.assertIn("BASE_KEY", auth)

    def test_mint_returns_aws_kind(self) -> None:
        cred = self._mint({"role_arn": "arn:aws:iam::123:role/r"})
        self.assertEqual(KIND_AWS, cred.kind)

    def test_mint_returns_all_three_values(self) -> None:
        cred = self._mint({"role_arn": "arn:aws:iam::123:role/r"}, sts_xml=_STS_XML)
        self.assertEqual("ASIA_NEW", cred.values["access_key_id"])
        self.assertEqual("new_secret", cred.values["secret_access_key"])
        self.assertEqual("new_token", cred.values["session_token"])

    def test_mint_has_expires_at(self) -> None:
        cred = self._mint({"role_arn": "arn:aws:iam::123:role/r"})
        self.assertIsNotNone(cred.expires_at)

    def test_mint_missing_role_arn_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "role_arn"):
            self.provider.mint("x", "b", {}, self.secrets)

    def test_mint_no_base_no_env_raises(self) -> None:
        with unittest.mock.patch.dict(os.environ,
                                      {"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""},
                                      clear=False):
            with self.assertRaisesRegex(ValueError, "base"):
                self.provider.mint("x", "b",
                                   {"role_arn": "arn:aws:iam::123:role/r"},
                                   self.secrets)

    def test_mint_no_base_uses_env(self) -> None:
        with unittest.mock.patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "ENV_KEY",
            "AWS_SECRET_ACCESS_KEY": "ENV_SECRET",
        }):
            cred = self._mint({"role_arn": "arn:aws:iam::123:role/r"})
        self.assertEqual(KIND_AWS, cred.kind)

    def test_describe(self) -> None:
        g = self.provider.describe({"role_arn": "arn:aws:iam::123:role/my-role"})
        self.assertEqual("my-role", g.acts_as)
        self.assertIn("STS", g.authority)
        self.assertIn("hour", g.lifetime)

    def test_source_name(self) -> None:
        self.assertEqual("assume_role", self.provider.source)

    def test_project_definable(self) -> None:
        self.assertTrue(self.provider.project_definable)

    def test_kind_is_aws(self) -> None:
        self.assertEqual((KIND_AWS,), self.provider.kinds)


class TestTopoSort(unittest.TestCase):
    def _sort(self, creds: dict[str, dict[str, object]]) -> list[str]:
        from bot_bottle.control.hostctl.cred_mint import _topo_sort
        return _topo_sort(creds)

    def test_no_deps_preserves_any_order(self) -> None:
        result = self._sort({"a": {}, "b": {}, "c": {}})
        self.assertEqual({"a", "b", "c"}, set(result))

    def test_dep_comes_before_dependent(self) -> None:
        result = self._sort({
            "role": {"base": "@base-aws", "role_arn": "arn:..."},
            "base-aws": {"access_key_id": "X"},
        })
        self.assertLess(result.index("base-aws"), result.index("role"))

    def test_chain(self) -> None:
        result = self._sort({
            "c": {"base": "@b"},
            "b": {"base": "@a"},
            "a": {},
        })
        self.assertLess(result.index("a"), result.index("b"))
        self.assertLess(result.index("b"), result.index("c"))

    def test_external_ref_ignored(self) -> None:
        # @ghost not in the dict — should not crash
        result = self._sort({"a": {"base": "@ghost"}})
        self.assertEqual(["a"], result)

    def test_non_cred_ref_sigils_ignored(self) -> None:
        result = self._sort({"a": {"token": "secret:MY_TOKEN", "msg": "?ask me"}})
        self.assertEqual(["a"], result)


if __name__ == "__main__":
    unittest.main()
