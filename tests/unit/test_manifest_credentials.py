"""Unit tests for bot_bottle.manifest.credentials and the credentials: block."""

from __future__ import annotations

import unittest
from typing import Any

from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.credentials import (
    ManifestCredential,
    check_cred_refs,
    cred_label,
    is_cred_ref,
    parse_cred_label,
    parse_credentials_block,
)
from bot_bottle.manifest.util import ManifestError


# ─── helpers ────────────────────────────────────────────────────────────────

def _bottle(d: dict[str, Any]) -> ManifestBottle:
    return ManifestBottle.from_dict("test", d)


def _creds(d: dict[str, Any], **kw: Any) -> dict[str, ManifestCredential]:
    return parse_credentials_block("test", d, **kw)


# ─── is_cred_ref / cred_label ───────────────────────────────────────────────

class TestCredRefDetection(unittest.TestCase):
    def test_at_label_is_cred_ref(self) -> None:
        self.assertTrue(is_cred_ref("@my-token"))
        self.assertTrue(is_cred_ref("@a"))
        self.assertTrue(is_cred_ref("@my-cred-123"))

    def test_dollar_forms_are_not_cred_refs(self) -> None:
        self.assertFalse(is_cred_ref("${MY_VAR}"))
        self.assertFalse(is_cred_ref("$my-token"))

    def test_double_at_escape_is_not_cred_ref(self) -> None:
        self.assertFalse(is_cred_ref("@@literal"))

    def test_literal_is_not_cred_ref(self) -> None:
        self.assertFalse(is_cred_ref("secret:NAME"))
        self.assertFalse(is_cred_ref("?message"))
        self.assertFalse(is_cred_ref("plain-string"))
        self.assertFalse(is_cred_ref(""))

    def test_uppercase_label_is_not_cred_ref(self) -> None:
        self.assertFalse(is_cred_ref("@MY_TOKEN"))

    def test_cred_label_happy(self) -> None:
        self.assertEqual(cred_label("@my-token"), "my-token")
        self.assertEqual(parse_cred_label("@a"), "a")

    def test_cred_label_non_ref(self) -> None:
        self.assertEqual(cred_label("${VAR}"), "")
        self.assertEqual(cred_label("literal"), "")


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


# ─── ManifestBottle integration ─────────────────────────────────────────────

class TestSettingsCredRefs(unittest.TestCase):
    """`@label` in a credential's settings: it must name a credential, and the
    references must not form a cycle."""

    def _check(self, creds: dict[str, Any]) -> None:
        check_cred_refs("b", _creds(creds), env_cred_vars={},
                        route_cred_labels=frozenset(), git_key_cred_labels=frozenset())

    def test_reference_to_declared_credential(self) -> None:
        self._check({
            "api": {"source": "secret", "secret": "GITEA_TOKEN"},
            "deploy": {"source": "gitea_deploy_key", "api_url": "https://g.example.com",
                       "token": "@api", "repo": "o/r"},
        })

    def test_reference_to_undeclared_credential(self) -> None:
        with self.assertRaises(ManifestError) as cm:
            self._check({"deploy": {"source": "gitea_deploy_key", "api_url": "https://g",
                                    "token": "@missing", "repo": "o/r"}})
        self.assertIn("@missing names no declared credential", str(cm.exception))

    def test_cycle(self) -> None:
        with self.assertRaises(ManifestError) as cm:
            self._check({
                "a": {"source": "host_login", "harness": "@b"},
                "b": {"source": "host_login", "harness": "@a"},
            })
        self.assertIn("credential cycle", str(cm.exception))


class TestSettingsValueForms(unittest.TestCase):
    """A non-secret source's string settings are checked as value forms."""

    def test_malformed_at_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "'@' must be followed by a credential label"):
            _creds({"k": {"source": "prompt", "message": "@BadLabel"}})

    def test_double_at_kept_as_written(self) -> None:
        # Unescaped where the value is used (`sigil_ref.literal`), not at load.
        creds = _creds({"k": {"source": "prompt", "message": "@@not-a-ref"}})
        self.assertEqual(creds["k"].settings["message"], "@@not-a-ref")

    def test_reserved_secret_name_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "reserved"):
            _creds({"k": {"source": "assume_role", "role_arn": "secret:BB_HOST_TOKEN"}})


class TestFileSettings(unittest.TestCase):
    def test_ssh_key_needs_private_key_path(self) -> None:
        with self.assertRaises(ManifestError):
            _creds({"k": {"source": "file"}})

    def test_ssh_key_public_key_path_is_optional(self) -> None:
        creds = _creds({"k": {"source": "file", "private_key_path": "~/.ssh/id"}})
        self.assertEqual(creds["k"].kind, "ssh-key")

    def test_token_needs_path(self) -> None:
        with self.assertRaises(ManifestError):
            _creds({"t": {"source": "file", "kind": "token"}})

    def test_resolved_kind_is_kept_in_settings(self) -> None:
        creds = _creds({"k": {"source": "file", "private_key_path": "~/.ssh/id"}})
        self.assertEqual(creds["k"].settings["kind"], "ssh-key")


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


class TestAssumeRoleCredentialManifest(unittest.TestCase):
    def test_valid_minimal(self) -> None:
        creds = _creds({"role": {
            "source": "assume_role",
            "role_arn": "arn:aws:iam::123456789012:role/my-role",
        }})
        c = creds["role"]
        self.assertEqual("assume_role", c.source)
        self.assertEqual("aws", c.kind)
        self.assertEqual("arn:aws:iam::123456789012:role/my-role", c.settings["role_arn"])

    def test_valid_with_base_ref(self) -> None:
        creds = _creds({
            "base-aws": {"source": "secret", "kind": "aws",
                         "access_key_id": "KEY_ID", "secret_access_key": "KEY_SEC"},
            "role": {"source": "assume_role",
                     "role_arn": "arn:aws:iam::123:role/r",
                     "base": "@base-aws"},
        })
        self.assertEqual("@base-aws", creds["role"].settings["base"])

    def test_valid_all_optional_settings(self) -> None:
        creds = _creds({"role": {
            "source": "assume_role",
            "role_arn": "arn:aws:iam::123:role/r",
            "session_name": "my-sess",
            "duration_seconds": 7200,
            "external_id": "ext-123",
            "region": "eu-west-1",
        }})
        s = creds["role"].settings
        self.assertEqual("my-sess", s["session_name"])
        self.assertEqual(7200, s["duration_seconds"])
        self.assertEqual("ext-123", s["external_id"])
        self.assertEqual("eu-west-1", s["region"])

    def test_missing_role_arn_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "role_arn"):
            _creds({"role": {"source": "assume_role"}})

    def test_base_non_at_ref_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "base"):
            _creds({"role": {
                "source": "assume_role",
                "role_arn": "arn:aws:iam::123:role/r",
                "base": "secret:SOME_KEY",
            }})

    def test_unknown_setting_raises(self) -> None:
        with self.assertRaisesRegex(ManifestError, "unknown setting"):
            _creds({"role": {
                "source": "assume_role",
                "role_arn": "arn:aws:iam::123:role/r",
                "extra": "nope",
            }})

    def test_kind_is_aws(self) -> None:
        creds = _creds({"role": {
            "source": "assume_role",
            "role_arn": "arn:aws:iam::123:role/r",
        }})
        self.assertEqual("aws", creds["role"].kind)

    def test_project_definable(self) -> None:
        creds = _creds({"role": {
            "source": "assume_role",
            "role_arn": "arn:aws:iam::123:role/r",
        }}, project_bottle=True)
        self.assertEqual("assume_role", creds["role"].source)

    def test_base_dep_validated_in_check_cred_refs(self) -> None:
        creds = _creds({"role": {
            "source": "assume_role",
            "role_arn": "arn:aws:iam::123:role/r",
            "base": "@ghost-cred",
        }})
        with self.assertRaisesRegex(ManifestError, "ghost-cred"):
            check_cred_refs("b", creds,
                            env_cred_vars={},
                            route_cred_labels=frozenset(),
                            git_key_cred_labels=frozenset())


if __name__ == "__main__":
    unittest.main()
