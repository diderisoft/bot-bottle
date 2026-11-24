"""Unit tests for bot_bottle.manifest.credentials and the credentials: block."""

from __future__ import annotations

import unittest

from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.credentials import (
    ManifestCredential,
    check_cred_refs,
    cred_label,
    is_cred_ref,
    parse_credentials_block,
)
from bot_bottle.manifest.util import ManifestError


# ─── helpers ────────────────────────────────────────────────────────────────

def _bottle(d: dict) -> ManifestBottle:
    return ManifestBottle.from_dict("test", d)


def _creds(d: dict, **kw) -> dict[str, ManifestCredential]:
    return parse_credentials_block("test", d, **kw)


# ─── is_cred_ref / cred_label ───────────────────────────────────────────────

class TestCredRefDetection(unittest.TestCase):
    def test_at_label_is_cred_ref(self):
        self.assertTrue(is_cred_ref("@my-token"))
        self.assertTrue(is_cred_ref("@a"))
        self.assertTrue(is_cred_ref("@my-cred-123"))

    def test_braced_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("${MY_VAR}"))

    def test_double_at_escape_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("@@literal"))

    def test_literal_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("secret:NAME"))
        self.assertFalse(is_cred_ref("?message"))
        self.assertFalse(is_cred_ref("plain-string"))
        self.assertFalse(is_cred_ref(""))

    def test_uppercase_label_is_not_cred_ref(self):
        self.assertFalse(is_cred_ref("@MY_TOKEN"))

    def test_cred_label_happy(self):
        self.assertEqual(cred_label("@my-token"), "my-token")
        self.assertEqual(cred_label("@a"), "a")

    def test_cred_label_non_ref(self):
        self.assertEqual(cred_label("${VAR}"), "")
        self.assertEqual(cred_label("literal"), "")

    def test_double_at_is_literal_escape(self):
        from bot_bottle.sigil_ref import parse, LITERAL
        self.assertEqual(parse("@@foo").kind, LITERAL)
        self.assertEqual(parse("@@").kind, LITERAL)


# ─── parse_credentials_block ────────────────────────────────────────────────

class TestParseCredentialsBlock(unittest.TestCase):
    def test_empty_is_fine(self):
        self.assertEqual(_creds({}), {})

    def test_none_is_fine(self):
        self.assertEqual(parse_credentials_block("b", None), {})

    def test_secret_token(self):
        creds = _creds({"my-token": {"source": "secret", "secret": "MY_TOKEN"}})
        c = creds["my-token"]
        self.assertEqual(c.source, "secret")
        self.assertEqual(c.kind, "token")
        self.assertEqual(c.settings["secret"], "MY_TOKEN")

    def test_secret_token_explicit_kind(self):
        creds = _creds({"tok": {"source": "secret", "kind": "token", "secret": "MY_TOK"}})
        self.assertEqual(creds["tok"].kind, "token")

    def test_secret_aws(self):
        creds = _creds({"aws-cred": {
            "source": "secret", "kind": "aws",
            "access_key_id": "AKI", "secret_access_key": "SAK",
        }})
        c = creds["aws-cred"]
        self.assertEqual(c.kind, "aws")
        self.assertEqual(c.settings["access_key_id"], "AKI")

    def test_secret_ssh_key(self):
        creds = _creds({"deploy-key": {
            "source": "secret", "kind": "ssh-key",
            "private_key": "PRIV", "public_key": "PUB",
        }})
        self.assertEqual(creds["deploy-key"].kind, "ssh-key")

    def test_prompt_source(self):
        creds = _creds({"api-key": {"source": "prompt", "message": "Enter your API key"}})
        c = creds["api-key"]
        self.assertEqual(c.source, "prompt")
        self.assertEqual(c.kind, "token")
        self.assertEqual(c.settings["message"], "Enter your API key")

    def test_agent_can_read_false_by_default(self):
        creds = _creds({"tok": {"source": "secret", "secret": "MY_TOKEN"}})
        self.assertFalse(creds["tok"].agent_can_read)

    def test_agent_can_read_true(self):
        creds = _creds({"tok": {"source": "secret", "secret": "MY_TOKEN",
                                "agent_can_read": True}})
        self.assertTrue(creds["tok"].agent_can_read)

    def test_unknown_source_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"tok": {"source": "does-not-exist", "secret": "X"}})

    def test_unknown_kind_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"tok": {"source": "secret", "kind": "magic-token", "secret": "X"}})

    def test_kind_mismatch_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"key": {"source": "prompt", "kind": "aws"}})

    def test_missing_source_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"tok": {"secret": "MY_TOKEN"}})

    def test_invalid_label_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"UPPER_CASE": {"source": "secret", "secret": "X"}})

    def test_project_bottle_project_definable_ok(self):
        creds = _creds({"tok": {"source": "secret", "secret": "MY_TOKEN"}},
                       project_bottle=True)
        self.assertEqual(creds["tok"].source, "secret")

    def test_project_bottle_agent_can_read_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"tok": {"source": "secret", "secret": "X", "agent_can_read": True}},
                   project_bottle=True)

    def test_secret_token_missing_secret_name_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"tok": {"source": "secret", "kind": "token"}})

    def test_secret_aws_missing_access_key_raises(self):
        with self.assertRaises(ManifestError):
            _creds({"cred": {"source": "secret", "kind": "aws",
                             "secret_access_key": "SAK"}})


# ─── check_cred_refs ─────────────────────────────────────────────────────────

class TestCheckCredRefs(unittest.TestCase):
    def _creds(self):
        return parse_credentials_block("b", {
            "my-token": {"source": "secret", "secret": "MY_TOKEN"},
            "my-key": {"source": "secret", "kind": "ssh-key",
                       "private_key": "PRIV", "public_key": "PUB"},
        })

    def test_valid_env_plus_route(self):
        creds = self._creds()
        check_cred_refs("b", creds,
                        env_cred_vars={"MY_API_KEY": "my-token"},
                        route_cred_labels=frozenset({"my-token"}),
                        git_key_cred_labels=frozenset())

    def test_undefined_label_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError):
            check_cred_refs("b", creds,
                            env_cred_vars={"X": "ghost-cred"},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset())

    def test_non_token_in_env_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError):
            check_cred_refs("b", creds,
                            env_cred_vars={"SSH_KEY": "my-key"},
                            route_cred_labels=frozenset({"my-key"}),
                            git_key_cred_labels=frozenset())

    def test_non_ssh_key_in_git_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError):
            check_cred_refs("b", creds,
                            env_cred_vars={},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset({"my-token"}))

    def test_unswappable_env_cred_raises(self):
        creds = self._creds()
        with self.assertRaises(ManifestError):
            check_cred_refs("b", creds,
                            env_cred_vars={"MY_API_KEY": "my-token"},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset())

    def test_settings_cred_ref_to_missing_label_raises(self):
        # A non-secret credential with @ghost in settings parses fine (valid sigil
        # syntax) but check_cred_refs rejects it because "ghost" is undeclared.
        creds = parse_credentials_block("b", {
            "tok": {"source": "prompt", "message": "@ghost"},
        })
        with self.assertRaisesRegex(ManifestError, "ghost"):
            check_cred_refs("b", creds,
                            env_cred_vars={},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset())

    def test_cycle_in_settings_raises(self):
        from bot_bottle.manifest.credentials import _check_cycles
        graph = {"a": frozenset({"b"}), "b": frozenset({"a"})}
        with self.assertRaisesRegex(ManifestError, "cycle"):
            _check_cycles("b", graph)

    def test_self_cycle_raises(self):
        from bot_bottle.manifest.credentials import _check_cycles
        graph = {"a": frozenset({"a"})}
        with self.assertRaisesRegex(ManifestError, "cycle"):
            _check_cycles("b", graph)

    def test_no_cycle_passes(self):
        from bot_bottle.manifest.credentials import _check_cycles
        graph = {"a": frozenset({"b"}), "b": frozenset(), "c": frozenset({"a"})}
        _check_cycles("b", graph)  # should not raise


# ─── ManifestBottle integration ─────────────────────────────────────────────

class TestManifestBottleCredentials(unittest.TestCase):
    def test_no_credentials_field(self):
        b = _bottle({})
        self.assertEqual(dict(b.credentials), {})

    def test_empty_credentials_block(self):
        b = _bottle({"credentials": {}})
        self.assertEqual(dict(b.credentials), {})

    def test_credential_declared_and_used_in_env_and_route(self):
        b = _bottle({
            "credentials": {
                "my-token": {"source": "secret", "secret": "MY_TOKEN"},
            },
            "env": {"MY_API_KEY": "@my-token"},
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "bearer", "value": "@my-token"},
            }]},
        })
        self.assertIn("my-token", b.credentials)
        self.assertEqual(b.env["MY_API_KEY"], "@my-token")

    def test_cred_ref_in_env_without_egress_route_raises(self):
        with self.assertRaises(ManifestError):
            _bottle({
                "credentials": {
                    "my-token": {"source": "secret", "secret": "MY_TOKEN"},
                },
                "env": {"MY_API_KEY": "@my-token"},
            })

    def test_undefined_cred_in_env_raises(self):
        with self.assertRaises(ManifestError):
            _bottle({
                "env": {"MY_API_KEY": "@ghost"},
                "egress": {"routes": [{
                    "host": "api.example.com",
                    "auth": {"type": "bearer", "value": "@ghost"},
                }]},
            })

    def test_cred_in_egress_auth_only_no_error(self):
        b = _bottle({
            "credentials": {
                "my-token": {"source": "secret", "secret": "MY_TOKEN"},
            },
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "bearer", "value": "@my-token"},
            }]},
        })
        self.assertIn("my-token", b.credentials)

    def test_braced_var_still_works(self):
        import os
        os.environ["_BB_TEST_VAR"] = "value"
        b = _bottle({"env": {"FOO": "${_BB_TEST_VAR}"}})
        self.assertEqual(b.env["FOO"], "${_BB_TEST_VAR}")
        del os.environ["_BB_TEST_VAR"]

    def test_egress_cred_label_stored(self):
        b = _bottle({
            "credentials": {
                "my-token": {"source": "secret", "secret": "MY_TOKEN"},
            },
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "bearer", "value": "@my-token"},
            }]},
        })
        route = b.egress.routes[0]
        self.assertEqual(route.CredLabel, "my-token")
        self.assertEqual(route.TokenRef, "")

    def test_egress_secret_ref_still_works(self):
        b = _bottle({
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "bearer", "value": "secret:MY_TOKEN"},
            }]},
        })
        route = b.egress.routes[0]
        self.assertEqual(route.TokenRef, "MY_TOKEN")
        self.assertEqual(route.CredLabel, "")

    def test_prompt_credential(self):
        b = _bottle({
            "credentials": {
                "api-key": {"source": "prompt", "message": "Enter API key"},
            },
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "bearer", "value": "@api-key"},
            }]},
        })
        self.assertEqual(b.credentials["api-key"].source, "prompt")

    def test_token_auth_header_cred_ref(self):
        b = _bottle({
            "credentials": {
                "my-token": {"source": "secret", "secret": "MY_TOKEN"},
            },
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "token", "value": "@my-token"},
            }]},
        })
        route = b.egress.routes[0]
        self.assertEqual(route.CredLabel, "my-token")
        self.assertEqual(route.AuthScheme, "token")

    def test_header_type_cred_ref(self):
        b = _bottle({
            "credentials": {
                "my-token": {"source": "secret", "secret": "MY_TOKEN"},
            },
            "egress": {"routes": [{
                "host": "api.example.com",
                "auth": {"type": "header", "name": "X-Api-Key", "value": "@my-token"},
            }]},
        })
        route = b.egress.routes[0]
        self.assertEqual(route.CredLabel, "my-token")
        self.assertEqual(route.AuthHeader, "x-api-key")

    def test_at_sigil_in_non_secret_setting_validates(self):
        # @bad-ref in a non-secret source setting fails at load
        with self.assertRaisesRegex(ManifestError, "@bad-ref"):
            _bottle({"credentials": {"k": {
                "source": "prompt",
                "message": "@bad-ref",   # @label ref to nonexistent cred
            }}})

    def test_bare_at_in_setting_raises(self):
        with self.assertRaisesRegex(ManifestError, "'@'"):
            _bottle({"credentials": {"k": {
                "source": "prompt",
                "message": "@BadLabel",  # uppercase — invalid sigil syntax
            }}})

    def test_double_at_escape_in_setting_is_literal(self):
        creds = _creds({"k": {"source": "prompt", "message": "@@not-a-ref"}})
        self.assertEqual(creds["k"].settings["message"], "@@not-a-ref")


class TestSettingsCarryResolvedKind(unittest.TestCase):
    """cred.settings always carries the resolved kind so provider.mint() can read it."""

    def test_explicit_kind_in_settings(self) -> None:
        creds = _creds({"tok": {"source": "file", "kind": "token",
                                "path": "/run/secrets/api-key"}})
        self.assertEqual("token", creds["tok"].settings["kind"])

    def test_implied_kind_in_settings(self) -> None:
        creds = _creds({"key": {"source": "file",
                                "private_key_path": "/priv",
                                "public_key_path": "/pub"}})
        self.assertEqual("ssh-key", creds["key"].settings["kind"])

    def test_profile_implied_aws_kind_in_settings(self) -> None:
        creds = _creds({"c": {"source": "profile"}})
        self.assertEqual("aws", creds["c"].settings["kind"])

    def test_secret_default_token_kind_in_settings(self) -> None:
        creds = _creds({"t": {"source": "secret", "secret": "MY_TOK"}})
        self.assertEqual("token", creds["t"].settings["kind"])


class TestFileCredentialManifest(unittest.TestCase):
    def test_file_ssh_key_valid(self) -> None:
        b = _bottle({"credentials": {"my-key": {
            "source": "file",
            "private_key_path": "/home/user/.ssh/id_ed25519",
            "public_key_path": "/home/user/.ssh/id_ed25519.pub",
        }}})
        cred = b.credentials["my-key"]
        self.assertEqual("file", cred.source)
        self.assertEqual("ssh-key", cred.kind)

    def test_file_token_valid(self) -> None:
        b = _bottle({"credentials": {"tok": {
            "source": "file", "kind": "token",
            "path": "/run/secrets/api-key",
        }}})
        self.assertEqual("token", b.credentials["tok"].kind)

    def test_file_ssh_key_missing_private_path_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "private_key_path"):
            _bottle({"credentials": {"k": {
                "source": "file",
                "public_key_path": "/pub",
            }}})

    def test_file_token_missing_path_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "'path: /path/to/file'"):
            _bottle({"credentials": {"k": {"source": "file", "kind": "token"}}})

    def test_file_unknown_setting_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "unknown setting"):
            _bottle({"credentials": {"k": {
                "source": "file",
                "private_key_path": "/priv", "public_key_path": "/pub",
                "extra": "nope",
            }}})

    def test_file_not_project_definable(self) -> None:
        with self.assertRaisesRegex(ManifestError, "project bottle"):
            _creds({"k": {
                "source": "file",
                "private_key_path": "/priv", "public_key_path": "/pub",
            }}, project_bottle=True)


class TestProfileCredentialManifest(unittest.TestCase):
    def test_profile_valid(self) -> None:
        b = _bottle({"credentials": {"creds": {"source": "profile"}}})
        cred = b.credentials["creds"]
        self.assertEqual("profile", cred.source)
        self.assertEqual("aws", cred.kind)

    def test_profile_with_profile_name(self) -> None:
        b = _bottle({"credentials": {"creds": {
            "source": "profile", "profile_name": "staging",
        }}})
        self.assertEqual("staging", b.credentials["creds"].settings["profile_name"])

    def test_profile_unknown_setting_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "unknown setting"):
            _bottle({"credentials": {"c": {"source": "profile", "region": "us-east-1"}}})

    def test_profile_not_project_definable(self) -> None:
        with self.assertRaisesRegex(ManifestError, "project bottle"):
            _creds({"c": {"source": "profile"}}, project_bottle=True)


if __name__ == "__main__":
    unittest.main()
