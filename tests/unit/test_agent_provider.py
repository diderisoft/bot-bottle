"""Unit: provider runtime defaults."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from bot_bottle.agent_provider import (
    CODEX_HOST_CREDENTIAL_HOSTS,
    agent_provision_plan,
    runtime_for,
)
from bot_bottle.egress import CODEX_HOST_CREDENTIAL_TOKEN_REF


def _jwt(exp: int) -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


class TestAgentProviderRuntime(unittest.TestCase):
    def test_claude_keeps_oauth_placeholder(self):
        runtime = runtime_for("claude")
        self.assertEqual("claude_code_oauth", runtime.auth_role)
        self.assertEqual("CLAUDE_CODE_OAUTH_TOKEN", runtime.placeholder_env)

    def test_codex_does_not_inject_openai_api_key_placeholder(self):
        runtime = runtime_for("codex")
        self.assertEqual("", runtime.auth_role)
        self.assertEqual("", runtime.placeholder_env)

    def test_codex_plan_declares_home_state(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="codex",
                dockerfile="/tmp/Dockerfile.codex",
                state_dir=Path(tmp),
            )
        self.assertEqual("codex", plan.template)
        self.assertEqual("codex", plan.command)
        self.assertEqual("read_prompt_file", plan.prompt_mode)
        self.assertEqual("/tmp/Dockerfile.codex", plan.dockerfile)
        self.assertEqual(
            "/etc/ssl/certs/ca-certificates.crt",
            plan.env_vars["CODEX_CA_CERTIFICATE"],
        )
        self.assertEqual({}, plan.guest_env)
        self.assertEqual(("/home/node/.codex",), tuple(d.guest_path for d in plan.dirs))
        self.assertEqual(
            ("/home/node/.codex/config.toml",),
            tuple(f.guest_path for f in plan.files),
        )

    def test_codex_forward_host_credentials_adds_auth_and_verify(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            home = Path(tmp) / "host-codex"
            home.mkdir()
            (home / "auth.json").write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": _jwt(2000000000)},
            }))
            plan = agent_provision_plan(
                template="codex",
                dockerfile="",
                state_dir=Path(tmp),
                guest_env={"CODEX_HOME": "/run/codex-home"},
                forward_host_credentials=True,
                host_env={"CODEX_HOME": str(home)},
            )
        self.assertIn(
            "/run/codex-home/auth.json",
            {f.guest_path for f in plan.files},
        )
        self.assertEqual("/run/codex-home", plan.env_vars["CODEX_HOME"])
        self.assertEqual(1, len(plan.pre_copy))
        self.assertEqual(1, len(plan.verify))
        self.assertIn("CODEX_HOME=/run/codex-home", plan.verify[0].argv)

    def test_claude_with_provider_auth_disables_nonessential_traffic(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="claude",
                dockerfile="/tmp/Dockerfile.claude",
                state_dir=Path(tmp),
                has_provider_auth=True,
            )
        self.assertEqual(
            "1",
            plan.env_vars["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"],
        )
        self.assertEqual("1", plan.env_vars["DISABLE_ERROR_REPORTING"])

    def test_codex_forward_host_credentials_populates_egress_routes(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            home = Path(tmp) / "host-codex"
            home.mkdir()
            (home / "auth.json").write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": _jwt(2000000000)},
            }))
            plan = agent_provision_plan(
                template="codex",
                dockerfile="",
                state_dir=Path(tmp),
                forward_host_credentials=True,
                host_env={"CODEX_HOME": str(home)},
            )
        hosts = [r.host for r in plan.egress_routes]
        self.assertEqual(sorted(CODEX_HOST_CREDENTIAL_HOSTS), sorted(hosts))
        for r in plan.egress_routes:
            self.assertEqual("Bearer", r.auth_scheme)
            self.assertEqual(CODEX_HOST_CREDENTIAL_TOKEN_REF, r.token_ref)
            self.assertTrue(r.tls_passthrough)

    def test_codex_without_forward_host_credentials_has_passthrough_egress_routes(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="codex",
                dockerfile="",
                state_dir=Path(tmp),
                forward_host_credentials=False,
            )
        self.assertEqual(
            {r.host for r in plan.egress_routes},
            set(CODEX_HOST_CREDENTIAL_HOSTS),
        )
        for r in plan.egress_routes:
            self.assertEqual("", r.auth_scheme)
            self.assertEqual("", r.token_ref)
            self.assertTrue(r.tls_passthrough)

    def test_claude_plan_has_no_egress_routes(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="claude",
                dockerfile="",
                state_dir=Path(tmp),
            )
        self.assertEqual((), plan.egress_routes)


if __name__ == "__main__":
    unittest.main()
