"""Tests for the AWS credential providers: assume_role and profile."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.credentials import (
    KIND_AWS,
    MintedCredential,
    _AssumeRoleProvider,
    _ProfileProvider,
    _parse_duration_secs,
)
from bot_bottle.manifest.credentials import parse_credentials_block
from bot_bottle.manifest.util import ManifestError
from bot_bottle.secret_source import MappingSecrets

_SECRETS = MappingSecrets({})


# ── _parse_duration_secs ──────────────────────────────────────────────────────

class TestParseDurationSecs(unittest.TestCase):
    def test_hours(self) -> None:
        self.assertEqual(_parse_duration_secs("2h", "x"), 7200)

    def test_minutes(self) -> None:
        self.assertEqual(_parse_duration_secs("30m", "x"), 1800)

    def test_seconds_suffix(self) -> None:
        self.assertEqual(_parse_duration_secs("3600s", "x"), 3600)

    def test_plain_integer(self) -> None:
        self.assertEqual(_parse_duration_secs("900", "x"), 900)

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            _parse_duration_secs("2d", "cred")


# ── _AssumeRoleProvider ───────────────────────────────────────────────────────

_STS_RESPONSE = json.dumps({
    "Credentials": {
        "AccessKeyId": "AKID-123",
        "SecretAccessKey": "SAK-456",
        "SessionToken": "ST-789",
        "Expiration": "2026-11-14T13:00:00Z",
    }
}).encode()


class TestAssumeRoleProvider(unittest.TestCase):

    def setUp(self) -> None:
        self.provider = _AssumeRoleProvider()
        self._settings: dict[str, Any] = {
            "role": "arn:aws:iam::123456789012:role/MyRole",
            "duration": "1h",
        }

    def _cmd(self, settings: dict[str, Any]) -> list[str]:
        with mock.patch("subprocess.check_output", return_value=_STS_RESPONSE) as m:
            self.provider.mint("r", "b", settings, _SECRETS)
        return list(m.call_args[0][0])

    def test_mint_happy_path(self) -> None:
        with mock.patch("subprocess.check_output", return_value=_STS_RESPONSE):
            cred = self.provider.mint("my-role", "bottle1", self._settings, _SECRETS)
        self.assertEqual(cred.kind, KIND_AWS)
        self.assertEqual(cred.values["access_key_id"], "AKID-123")
        self.assertEqual(cred.values["secret_access_key"], "SAK-456")
        self.assertEqual(cred.values["session_token"], "ST-789")
        self.assertIsNotNone(cred.expires_at)
        self.assertEqual(cred.principal, "assume_role:arn:aws:iam::123456789012:role/MyRole")

    def test_mint_includes_duration_in_command(self) -> None:
        cmd = self._cmd({**self._settings, "duration": "30m"})
        self.assertEqual(cmd[cmd.index("--duration-seconds") + 1], "1800")

    def test_mint_includes_profile_when_set(self) -> None:
        cmd = self._cmd({**self._settings, "profile": "myprofile"})
        self.assertEqual(cmd[cmd.index("--profile") + 1], "myprofile")

    def test_mint_passes_session_policy_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            policy = Path(d) / "policy.json"
            policy.write_text('{"Version": "2012-10-17"}')
            cmd = self._cmd({**self._settings, "session_policy": str(policy)})
        self.assertEqual(cmd[cmd.index("--policy") + 1], '{"Version": "2012-10-17"}')

    def test_mint_missing_role_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.mint("r", "b", {}, _SECRETS)

    def test_mint_aws_cli_not_found_raises(self) -> None:
        with mock.patch("subprocess.check_output",
                        side_effect=FileNotFoundError("aws not found")):
            with self.assertRaisesRegex(ValueError, "'aws' CLI not found"):
                self.provider.mint("r", "b", self._settings, _SECRETS)

    def test_mint_aws_cli_error_raises(self) -> None:
        err = subprocess.CalledProcessError(1, "aws", stderr=b"AccessDenied")
        with mock.patch("subprocess.check_output", side_effect=err):
            with self.assertRaisesRegex(ValueError, "AccessDenied"):
                self.provider.mint("r", "b", self._settings, _SECRETS)

    def test_session_policy_file_not_found_raises(self) -> None:
        with mock.patch("subprocess.check_output"):
            with self.assertRaisesRegex(ValueError, "session_policy file not found"):
                self.provider.mint("r", "b", {**self._settings,
                                              "session_policy": "/nonexistent/policy.json"},
                                   _SECRETS)

    def test_project_definable(self) -> None:
        self.assertTrue(self.provider.project_definable)

    def test_describe(self) -> None:
        g = self.provider.describe(self._settings)
        self.assertIn("MyRole", g.acts_as)
        self.assertIn("123456789012", g.authority)
        self.assertEqual(g.starts_from, "default AWS credentials")

    def test_describe_with_profile_and_policy(self) -> None:
        g = self.provider.describe({**self._settings, "profile": "p",
                                    "session_policy": "~/policy.json"})
        self.assertIn("'p'", g.starts_from)
        self.assertIn("~/policy.json", g.scope)


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
            "source": "assume_role", "role": "arn:aws:iam::1:role/R",
            "profile": "p", "duration": "30m", "session_policy": "~/p.json"}})
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
