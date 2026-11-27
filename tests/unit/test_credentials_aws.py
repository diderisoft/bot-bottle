"""Tests for the AWS `profile` credential provider, and the aws manifest
settings. The STS `assume_role` provider is tested in test_credentials.py."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from bot_bottle.credentials import (
    KIND_AWS,
    MintedCredential,
    _ProfileProvider,
)
from bot_bottle.manifest.credentials import parse_credentials_block
from bot_bottle.manifest.util import ManifestError
from bot_bottle.secret_source import MappingSecrets

_SECRETS = MappingSecrets({})


# ── _ProfileProvider ──────────────────────────────────────────────────────────

_PROFILE_RESPONSE = json.dumps({
    "AccessKeyId": "PROFILE-AKI",
    "SecretAccessKey": "PROFILE-SAK",
    "SessionToken": "PROFILE-ST",
    "Expiration": "2026-11-14T13:00:00Z",
}).encode()

_PROFILE_NO_EXPIRY = json.dumps({
    "AccessKeyId": "STATIC-AKI",
    "SecretAccessKey": "STATIC-SAK",
}).encode()


class TestProfileProvider(unittest.TestCase):

    def setUp(self) -> None:
        self.provider = _ProfileProvider()

    def test_mint_happy_path(self) -> None:
        with mock.patch("subprocess.check_output", return_value=_PROFILE_RESPONSE) as m:
            cred = self.provider.mint("aws", "bottle1", {"profile": "myprofile"}, _SECRETS)
        self.assertEqual(m.call_args[0][0], ["aws", "configure", "export-credentials",
                                             "--profile", "myprofile", "--format", "process"])
        self.assertEqual(cred.kind, KIND_AWS)
        self.assertEqual(cred.values["access_key_id"], "PROFILE-AKI")
        self.assertEqual(cred.values["session_token"], "PROFILE-ST")
        self.assertIsNotNone(cred.expires_at)

    def test_mint_no_expiry_in_response(self) -> None:
        with mock.patch("subprocess.check_output", return_value=_PROFILE_NO_EXPIRY):
            cred = self.provider.mint("aws", "b", {"profile": "static"}, _SECRETS)
        self.assertIsNone(cred.expires_at)
        self.assertNotIn("session_token", cred.values)

    def test_mint_missing_profile_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.mint("aws", "b", {}, _SECRETS)

    def test_mint_aws_cli_not_found_raises(self) -> None:
        with mock.patch("subprocess.check_output",
                        side_effect=FileNotFoundError("aws not found")):
            with self.assertRaisesRegex(ValueError, "'aws' CLI not found"):
                self.provider.mint("aws", "b", {"profile": "p"}, _SECRETS)

    def test_mint_aws_cli_error_raises(self) -> None:
        err = subprocess.CalledProcessError(255, "aws", stderr=b"The SSO session has expired")
        with mock.patch("subprocess.check_output", side_effect=err):
            with self.assertRaisesRegex(ValueError, "SSO session has expired"):
                self.provider.mint("aws", "b", {"profile": "p"}, _SECRETS)

    def test_renew_re_reads_profile(self) -> None:
        old = MintedCredential(KIND_AWS, {"access_key_id": "OLD"}, None, "profile:p")
        new_resp = json.dumps({
            "AccessKeyId": "RENEWED-AKI",
            "SecretAccessKey": "RENEWED-SAK",
        }).encode()
        with mock.patch("subprocess.check_output", return_value=new_resp):
            fresh = self.provider.renew("aws", "b", old, {"profile": "p"}, _SECRETS)
        self.assertEqual(fresh.values["access_key_id"], "RENEWED-AKI")

    def test_not_project_definable(self) -> None:
        self.assertFalse(self.provider.project_definable)

    def test_describe(self) -> None:
        g = self.provider.describe({"profile": "my-profile"})
        self.assertIn("my-profile", g.starts_from)


# ── manifest settings ─────────────────────────────────────────────────────────

class TestAwsManifestSettings(unittest.TestCase):

    def test_assume_role_settings_accepted(self) -> None:
        creds = parse_credentials_block("b", {"aws": {
            "source": "assume_role", "role_arn": "arn:aws:iam::1:role/R",
            "base": "@base-aws", "session_name": "s", "duration_seconds": 1800,
            "external_id": "x", "region": "us-west-2"}})
        self.assertEqual(creds["aws"].kind, KIND_AWS)

    def test_profile_settings_accepted(self) -> None:
        creds = parse_credentials_block("b", {"aws": {"source": "profile", "profile": "p"}})
        self.assertEqual(creds["aws"].kind, KIND_AWS)

    def test_unknown_setting_refused(self) -> None:
        with self.assertRaises(ManifestError):
            parse_credentials_block("b", {"aws": {"source": "profile",
                                                  "profile_name": "p"}})


if __name__ == "__main__":
    unittest.main()
