"""Unit tests for bot_bottle.manifest.credentials and the credentials: block."""

import textwrap
import unittest

import yaml

from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.credentials import (
    ManifestCredential,
    check_cred_refs,
    is_cred_ref,
    parse_cred_label,
    parse_credentials_block,
    unescape_dollar,
)
from bot_bottle.manifest.util import ManifestError


# ─── helpers ────────────────────────────────────────────────────────────────

def _bottle(text: str) -> ManifestBottle:
    """Parse a YAML bottle definition via ManifestBottle.from_dict."""
    d = yaml.safe_load(textwrap.dedent(text))
    return ManifestBottle.from_dict("test", d or {})


def _creds(text: str, **kw) -> dict[str, ManifestCredential]:
    d = yaml.safe_load(textwrap.dedent(text))
    return parse_credentials_block("test", d, **kw)


# ─── is_cred_ref / parse_cred_label ─────────────────────────────────────────

class TestCredRefDetection(unittest.TestCase):
    def test_bare_dollar_label(self):
        self.assertTrue(is_cred_ref("$my-token"))
        self.assertTrue(is_cred_ref("$a"))
        self.assertTrue(is_cred_ref("$my-cred-123"))

    def test_braced_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("${MY_VAR}"))
        self.assertFalse(is_cred_ref("${my-token}"))

    def test_double_dollar_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("$$literal"))

    def test_literal_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("secret:NAME"))
        self.assertFalse(is_cred_ref("?message"))
        self.assertFalse(is_cred_ref("plain-string"))
        self.assertFalse(is_cred_ref(""))

    def test_uppercase_label_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("$MY_TOKEN"))

    def test_parse_cred_label_happy(self):
        self.assertEqual(parse_cred_label("$my-token"), "my-token")
        self.assertEqual(parse_cred_label("$a"), "a")

    def test_parse_cred_label_non_ref(self):
        self.assertEqual(parse_cred_label("${VAR}"), "")
        self.assertEqual(parse_cred_label("literal"), "")

    def test_unescape_dollar(self):
        self.assertEqual(unescape_dollar("$$literal"), "$literal")
        self.assertEqual(unescape_dollar("$label"), "$label")
        self.assertEqual(unescape_dollar("hello"), "hello")


# ─── parse_credentials_block ────────────────────────────────────────────────

class TestParseCredentialsBlock(unittest.TestCase):
    def test_empty_is_fine(self):
        self.assertEqual(_creds("{}"), {})

    def test_none_is_fine(self):
        self.assertEqual(parse_credentials_block("b", None), {})

    def test_secret_token(self):
        creds = _creds("""
            my-token:
              source: secret
              secret: MY_TOKEN
        """)
        c = creds["my-token"]
        self.assertEqual(c.source, "secret")
        self.assertEqual(c.kind, "token")
        self.assertEqual(c.settings["secret"], "MY_TOKEN")

    def test_secret_token_explicit_kind(self):
        creds = _creds("""
            tok:
              source: secret
              kind: token
              secret: MY_TOK
        """)
        self.assertEqual(creds["tok"].kind, "token")

    def test_secret_aws(self):
        creds = _creds("""
            aws-cred:
              source: secret
              kind: aws
              access_key_id: AKI
              secret_access_key: SAK
        """)
        c = creds["aws-cred"]
        self.assertEqual(c.kind, "aws")
        self.assertEqual(c.settings["access_key_id"], "AKI")

    def test_secret_ssh_key(self):
        creds = _creds("""
            deploy-key:
              source: secret
              kind: ssh-key
              private_key: PRIV
              public_key: PUB
        """)
        self.assertEqual(creds["deploy-key"].kind, "ssh-key")

    def test_prompt_source(self):
        creds = _creds("""
            api-key:
              source: prompt
              message: "Enter your API key"
        """)
        c = creds["api-key"]
        self.assertEqual(c.source, "prompt")
        self.assertEqual(c.kind, "token")
        self.assertEqual(c.settings["message"], "Enter your API key")

    def test_agent_can_read_false_by_default(self):
        creds = _creds("""
            tok:
              source: secret
              secret: MY_TOKEN
        """)
        self.assertFalse(creds["tok"].agent_can_read)

    def test_agent_can_read_true(self):
        creds = _creds("""
            tok:
              source: secret
              secret: MY_TOKEN
              agent_can_read: true
        """)
        self.assertTrue(creds["tok"].agent_can_read)

    def test_unknown_source_raises(self):
        with self.assertRaises(ManifestError, msg="unknown source"):
            _creds("""
                tok:
                  source: does-not-exist
                  secret: X
            """)

    def test_unknown_kind_raises(self):
        with self.assertRaises(ManifestError, msg="unknown kind"):
            _creds("""
                tok:
                  source: secret
                  kind: magic-token
                  secret: X
            """)

    def test_kind_mismatch_raises(self):
        with self.assertRaises(ManifestError, msg="cannot mint"):
            _creds("""
                key:
                  source: prompt
                  kind: aws
            """)

    def test_missing_source_raises(self):
        with self.assertRaises(ManifestError, msg="source required"):
            _creds("""
                tok:
                  secret: MY_TOKEN
            """)

    def test_invalid_label_raises(self):
        with self.assertRaises(ManifestError, msg="label must be kebab-case"):
            _creds("""
                UPPER_CASE:
                  source: secret
                  secret: X
            """)

    def test_project_bottle_project_definable_ok(self):
        creds = _creds("""
            tok:
              source: secret
              secret: MY_TOKEN
        """, project_bottle=True)
        self.assertEqual(creds["tok"].source, "secret")

    def test_project_bottle_agent_can_read_raises(self):
        with self.assertRaises(ManifestError, msg="project bottle"):
            _creds("""
                tok:
                  source: secret
                  secret: X
                  agent_can_read: true
            """, project_bottle=True)

    def test_secret_token_missing_secret_name_raises(self):
        with self.assertRaises(ManifestError):
            _creds("""
                tok:
                  source: secret
                  kind: token
            """)

    def test_secret_aws_missing_access_key_raises(self):
        with self.assertRaises(ManifestError):
            _creds("""
                cred:
                  source: secret
                  kind: aws
                  secret_access_key: SAK
            """)


# ─── check_cred_refs ─────────────────────────────────────────────────────────

class TestCheckCredRefs(unittest.TestCase):
    def _creds(self):
        return parse_credentials_block("b", yaml.safe_load("""
            my-token:
              source: secret
              secret: MY_TOKEN
            my-key:
              source: secret
              kind: ssh-key
              private_key: PRIV
              public_key: PUB
        """))

    def test_valid_env_plus_route(self):
        creds = self._creds()
        check_cred_refs("b", creds,
                        env_cred_vars={"MY_API_KEY": "my-token"},
                        route_cred_labels=frozenset({"my-token"}),
                        git_key_cred_labels=frozenset())

    def test_undefined_label_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError, msg="not declared"):
            check_cred_refs("b", creds,
                            env_cred_vars={"X": "ghost-cred"},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset())

    def test_non_token_in_env_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError, msg="env vars only accept"):
            check_cred_refs("b", creds,
                            env_cred_vars={"SSH_KEY": "my-key"},
                            route_cred_labels=frozenset({"my-key"}),
                            git_key_cred_labels=frozenset())

    def test_non_ssh_key_in_git_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError, msg="git-gate keys require"):
            check_cred_refs("b", creds,
                            env_cred_vars={},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset({"my-token"}))

    def test_unswappable_env_cred_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError, msg="never swapped"):
            check_cred_refs("b", creds,
                            env_cred_vars={"MY_API_KEY": "my-token"},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset())


# ─── ManifestBottle integration ─────────────────────────────────────────────

class TestManifestBottleCredentials(unittest.TestCase):
    def test_no_credentials_field(self):
        b = _bottle("")
        self.assertEqual(dict(b.credentials), {})

    def test_empty_credentials_block(self):
        b = _bottle("credentials: {}")
        self.assertEqual(dict(b.credentials), {})

    def test_credential_declared_and_used_in_env_and_route(self):
        b = _bottle("""
            credentials:
              my-token:
                source: secret
                secret: MY_TOKEN
            env:
              MY_API_KEY: $my-token
            egress:
              routes:
                - host: api.example.com
                  auth:
                    type: bearer
                    value: $my-token
        """)
        self.assertIn("my-token", b.credentials)
        self.assertEqual(b.env["MY_API_KEY"], "$my-token")

    def test_cred_ref_in_env_without_egress_route_raises(self):
        with self.assertRaises(ManifestError, msg="never swapped"):
            _bottle("""
                credentials:
                  my-token:
                    source: secret
                    secret: MY_TOKEN
                env:
                  MY_API_KEY: $my-token
            """)

    def test_undefined_cred_in_env_raises(self):
        with self.assertRaises(ManifestError):
            _bottle("""
                env:
                  MY_API_KEY: $ghost
                egress:
                  routes:
                    - host: api.example.com
                      auth:
                        type: bearer
                        value: $ghost
            """)

    def test_cred_in_egress_auth_only_no_error(self):
        # Route with $label but no env var — the placeholder is in the route
        # only, so check_cred_refs should pass with no env vars.
        b = _bottle("""
            credentials:
              my-token:
                source: secret
                secret: MY_TOKEN
            egress:
              routes:
                - host: api.example.com
                  auth:
                    type: bearer
                    value: $my-token
        """)
        # Route has CredLabel set, no env var → no unswappable check needed.
        self.assertIn("my-token", b.credentials)

    def test_braced_var_still_works(self):
        import os
        os.environ["_BB_TEST_VAR"] = "value"
        b = _bottle("env:\n  FOO: ${_BB_TEST_VAR}")
        self.assertEqual(b.env["FOO"], "${_BB_TEST_VAR}")
        del os.environ["_BB_TEST_VAR"]

    def test_egress_cred_label_stored(self):
        b = _bottle("""
            credentials:
              my-token:
                source: secret
                secret: MY_TOKEN
            egress:
              routes:
                - host: api.example.com
                  auth:
                    type: bearer
                    value: $my-token
        """)
        route = b.egress.routes[0]
        self.assertEqual(route.CredLabel, "my-token")
        self.assertEqual(route.TokenRef, "")

    def test_egress_secret_ref_still_works(self):
        b = _bottle("""
            egress:
              routes:
                - host: api.example.com
                  auth:
                    type: bearer
                    value: secret:MY_TOKEN
        """)
        route = b.egress.routes[0]
        self.assertEqual(route.TokenRef, "MY_TOKEN")
        self.assertEqual(route.CredLabel, "")

    def test_prompt_credential(self):
        b = _bottle("""
            credentials:
              api-key:
                source: prompt
                message: "Enter API key"
            egress:
              routes:
                - host: api.example.com
                  auth:
                    type: bearer
                    value: $api-key
        """)
        self.assertEqual(b.credentials["api-key"].source, "prompt")


if __name__ == "__main__":
    unittest.main()
