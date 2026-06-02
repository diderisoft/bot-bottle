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

    def test_claude_with_auth_token_injects_provider_route_and_placeholder(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="claude",
                dockerfile="/tmp/Dockerfile.claude",
                state_dir=Path(tmp),
                auth_token="BOT_BOTTLE_CLAUDE_OAUTH_TOKEN",
            )
        self.assertEqual(1, len(plan.egress_routes))
        route = plan.egress_routes[0]
        self.assertEqual("api.anthropic.com", route.host)
        self.assertEqual("Bearer", route.auth_scheme)
        self.assertEqual("BOT_BOTTLE_CLAUDE_OAUTH_TOKEN", route.token_ref)
        self.assertTrue(route.tls_passthrough)
        self.assertEqual("egress-placeholder", plan.env_vars["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual("1", plan.env_vars["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"])
        self.assertEqual("1", plan.env_vars["DISABLE_ERROR_REPORTING"])
        self.assertEqual(frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}), plan.hidden_env_names)

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

    def test_claude_without_auth_token_has_passthrough_egress_route(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="claude",
                dockerfile="",
                state_dir=Path(tmp),
            )
        self.assertEqual(1, len(plan.egress_routes))
        route = plan.egress_routes[0]
        self.assertEqual("api.anthropic.com", route.host)
        self.assertEqual("", route.auth_scheme)
        self.assertEqual("", route.token_ref)
        self.assertTrue(route.tls_passthrough)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", plan.env_vars)
        self.assertEqual(frozenset(), plan.hidden_env_names)

    def test_codex_forward_host_credentials_populates_provisioned_env(self):
        access = _jwt(2000000000)
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            home = Path(tmp) / "host-codex"
            home.mkdir()
            (home / "auth.json").write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": access},
            }))
            plan = agent_provision_plan(
                template="codex",
                dockerfile="",
                state_dir=Path(tmp),
                forward_host_credentials=True,
                host_env={"CODEX_HOME": str(home)},
            )
        self.assertEqual(
            {CODEX_HOST_CREDENTIAL_TOKEN_REF: access},
            plan.provisioned_env,
        )

    def test_codex_without_forward_host_credentials_has_empty_provisioned_env(self):
        with tempfile.TemporaryDirectory(prefix="bb-provider.") as tmp:
            plan = agent_provision_plan(
                template="codex",
                dockerfile="",
                state_dir=Path(tmp),
                forward_host_credentials=False,
            )
        self.assertEqual({}, plan.provisioned_env)


if __name__ == "__main__":
    unittest.main()
