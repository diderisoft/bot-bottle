"""Keeping running bottles' forwarded host logins current."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle.agent_provider import CredentialRefresh, CredentialRefreshSpec
from bot_bottle.backend import adoption
from bot_bottle.backend.adoption import AdoptedCredentials, AdoptionRecord
from bot_bottle.contrib.claude.agent_provider import ClaudeAgentProvider
from bot_bottle.contrib.codex.agent_provider import CodexAgentProvider
from bot_bottle.control.hostctl import credential_refresh
from bot_bottle.control.hostctl.credential_refresh import CredentialRefresher
from bot_bottle.egress import CLAUDE_HOST_CREDENTIAL_TOKEN_REF, CODEX_HOST_CREDENTIAL_TOKEN_REF
from bot_bottle.errors import HostLoginError
from tests.unit import use_bottle_root
from tests.unit.test_codex_auth import _jwt

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
REF = CLAUDE_HOST_CREDENTIAL_TOKEN_REF
CREDS = AdoptedCredentials("claude", (REF,), slots={REF: ("EGRESS_TOKEN_0", "EGRESS_TOKEN_1")})


class _Bottle:
    """A stand-in Bottle whose `exec` answers `secret` (the guest's key, or
    the guest's auth.json)."""

    def __init__(self, secret: str = "evs") -> None:
        self.secret = secret
        self.execs: list[str] = []

    def exec(self, script: str, *, user: str = "node") -> Any:
        del user
        self.execs.append(script)
        return SimpleNamespace(returncode=0, stdout=self.secret + "\n", stderr="")


class _Provider:
    def __init__(self) -> None:
        self.result: CredentialRefresh | Exception = CredentialRefresh(
            {REF: "tok-1"}, NOW + timedelta(days=5))

    def refresh_credentials(self, spec: CredentialRefreshSpec, bottle: Any,
                            host_env: Any = None) -> CredentialRefresh:
        del spec, bottle, host_env
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TestRefresher(unittest.TestCase):
    def setUp(self) -> None:
        self.pushed: list[tuple[str, str, str, str]] = []
        self.audit = mock.Mock()
        self.provider = _Provider()
        self.enterContext(mock.patch("bot_bottle.agent_provider.get_provider",
                                     return_value=self.provider))
        def push(registry_id: str, slot: str, value: str, key: str) -> None:
            self.pushed.append((registry_id, slot, value, key))
        self.refresher = CredentialRefresher(push, audit=self.audit, clock=lambda: NOW)
        self.warnings: list[str] = []
        self.enterContext(mock.patch.object(credential_refresh.log, "warn",
                                            side_effect=self.warnings.append))
        self.enterContext(mock.patch.object(credential_refresh.log, "info"))

    def test_a_changed_login_goes_to_every_slot_once(self) -> None:
        bottle = _Bottle()
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, bottle),
                           env_var_secret="key",
                           current={REF: "tok-0"})
        self.refresher.refresh_all()
        self.assertEqual([("r1", "EGRESS_TOKEN_0", "tok-1", "key"),
                          ("r1", "EGRESS_TOKEN_1", "tok-1", "key")], self.pushed)
        self.refresher.refresh_all()  # unchanged: nothing more
        self.assertEqual(2, len(self.pushed))
        self.assertEqual([], bottle.execs)  # the key was known
        kind = self.audit.credential_event.call_args.args[0]
        self.assertEqual("credential.renewed", kind)
        self.assertNotIn("tok-1", repr(self.audit.credential_event.call_args))

    def test_the_launch_value_isnt_pushed_again(self) -> None:
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, _Bottle()),
                           current={REF: "tok-1"})
        self.refresher.refresh_all()
        self.assertEqual([], self.pushed)

    def test_an_adopted_bottle_reads_its_key_from_inside(self) -> None:
        bottle = _Bottle(secret="from-guest")
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, bottle))
        self.refresher.refresh_all()
        self.assertEqual("from-guest", self.pushed[0][3])
        self.assertIn("env-var-secret", bottle.execs[0])

    def test_a_missing_login_warns_once(self) -> None:
        self.provider.result = HostLoginError("run `claude` on the host")
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, _Bottle()))
        self.refresher.refresh_all()
        self.refresher.refresh_all()
        self.assertEqual(["bottle b1: run `claude` on the host"], self.warnings)
        self.assertEqual(1, self.audit.credential_event.call_count)
        self.assertEqual("credential.renew_failed", self.audit.credential_event.call_args.args[0])
        self.assertEqual([], self.pushed)

    def test_a_login_near_expiry_warns(self) -> None:
        self.provider.result = CredentialRefresh({REF: "tok-1"}, NOW + timedelta(hours=2))
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, _Bottle()),
                           current={REF: "tok-1"})
        self.refresher.refresh_all()
        self.assertEqual(1, len(self.warnings))
        self.assertIn("expires at 2026-10-26 14:00 UTC; run `claude` on the host", self.warnings[0])

    def test_one_failing_bottle_doesnt_stop_the_others(self) -> None:
        def push(*args: str) -> None:
            if args[0] == "r1":
                raise RuntimeError("orchestrator down")
            self.pushed.append(args)  # type: ignore[arg-type]
        self.refresher._push = push  # pylint: disable=protected-access
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, _Bottle()))
        self.refresher.add("b2", CREDS, registry_id="r2", bottle=cast(Any, _Bottle()))
        self.refresher.refresh_all()
        self.assertEqual({"r2"}, {p[0] for p in self.pushed})
        self.assertIn("orchestrator down", self.warnings[0])
        self.refresher.remove("b2")
        self.pushed.clear()
        self.refresher.refresh_all()
        self.assertEqual([], self.pushed)


class TestRefreshNow(TestRefresher):
    def test_reports_what_happened(self) -> None:
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, _Bottle()),
                           current={REF: "tok-0"})
        self.assertEqual({"refreshed": True, "expires_at": "2026-10-31T12:00:00+00:00"},
                         self.refresher.refresh_now("b1"))
        self.assertFalse(self.refresher.refresh_now("b1")["refreshed"])
        self.provider.result = HostLoginError("run `claude` on the host")
        self.assertEqual({"error": "run `claude` on the host"}, self.refresher.refresh_now("b1"))

    def test_a_bottle_without_a_forwarded_login(self) -> None:
        self.assertIn("Stop and Resume it", str(self.refresher.refresh_now("nope")))

    def test_a_failed_push(self) -> None:
        def push(*_args: str) -> None:
            raise RuntimeError("orchestrator down")
        self.refresher._push = push  # pylint: disable=protected-access
        self.refresher.add("b1", CREDS, registry_id="r1", bottle=cast(Any, _Bottle()))
        self.assertIn("orchestrator down", str(self.refresher.refresh_now("b1")["error"]))


class TestAdoptionRecord(unittest.TestCase):
    def test_credentials_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            restore = use_bottle_root(Path(tmp))
            try:
                adoption.write("b1", AdoptionRecord(container="c", registry_id="r1",
                                                    credentials=CREDS))
                self.assertEqual(CREDS, adoption.read("b1").credentials)  # type: ignore[union-attr]
                adoption.write("b2", AdoptionRecord(container="c", registry_id="r2"))
                self.assertIsNone(adoption.read("b2").credentials)  # type: ignore[union-attr]
            finally:
                restore()

    def test_credentials_for_maps_refs_to_slots(self) -> None:
        plan = SimpleNamespace(
            harness="claude",
            agent_provision=SimpleNamespace(provisioned_env={REF: "x"}),
            egress_plan=SimpleNamespace(token_env_map={
                "EGRESS_TOKEN_0": REF, "EGRESS_TOKEN_1": "OTHER", "EGRESS_TOKEN_2": REF}))
        got = adoption.credentials_for(plan)  # type: ignore[arg-type]
        self.assertEqual(AdoptedCredentials("claude", (REF,), "",
                                            {REF: ("EGRESS_TOKEN_0", "EGRESS_TOKEN_2")}), got)
        plan.agent_provision.provisioned_env = {}
        self.assertIsNone(adoption.credentials_for(plan))  # type: ignore[arg-type]


class TestProviders(unittest.TestCase):
    def test_claude_rereads_the_host_login(self) -> None:
        provider = ClaudeAgentProvider()
        with tempfile.TemporaryDirectory() as home:
            creds = Path(home) / ".claude" / ".credentials.json"
            creds.parent.mkdir()
            exp_ms = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp() * 1000)
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a2",
                                                           "expiresAt": exp_ms}}))
            spec = CredentialRefreshSpec("claude", (REF,))
            got = provider.refresh_credentials(spec, cast(Any, _Bottle()), {"HOME": home})
            self.assertEqual({REF: "a2"}, dict(got.values))
            self.assertIsNotNone(got.expires_at)
            creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a2",
                                                           "expiresAt": 1000}}))
            with self.assertRaisesRegex(HostLoginError, "expired"):
                provider.refresh_credentials(spec, cast(Any, _Bottle()), {"HOME": home})

    def test_codex_rewrites_the_guest_placeholder_when_expiry_moves(self) -> None:
        provider = CodexAgentProvider()
        exp = int((datetime.now(timezone.utc) + timedelta(days=9)).timestamp())
        with tempfile.TemporaryDirectory() as codex_home:
            (Path(codex_home) / "auth.json").write_text(json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {"access_token": _jwt(exp), "refresh_token": "r"}}))
            spec = CredentialRefreshSpec("codex", (CODEX_HOST_CREDENTIAL_TOKEN_REF,),
                                         guest_dir="/home/node/.codex")
            bottle = _Bottle(secret="{}")  # the guest's placeholder: stale
            got = provider.refresh_credentials(spec, cast(Any, bottle), {"CODEX_HOME": codex_home})
            self.assertEqual({CODEX_HOST_CREDENTIAL_TOKEN_REF: _jwt(exp)}, dict(got.values))
            self.assertEqual(2, len(bottle.execs))  # read, then rewrite
            self.assertIn("/home/node/.codex/auth.json", bottle.execs[1])
            self.assertNotIn('"r"', bottle.execs[1])  # the refresh token never goes in
            # A placeholder that already carries the expiry is left alone.
            from bot_bottle.contrib.codex.codex_auth import (  # pylint: disable=import-outside-toplevel
                codex_dummy_auth_json)
            current = _Bottle(secret=codex_dummy_auth_json({"CODEX_HOME": codex_home}))
            provider.refresh_credentials(spec, cast(Any, current), {"CODEX_HOME": codex_home})
            self.assertEqual(1, len(current.execs))

    def test_no_spec_without_a_forwarded_login(self) -> None:
        plan = SimpleNamespace(agent_provision=SimpleNamespace(provisioned_env={}))
        for provider in (ClaudeAgentProvider(), CodexAgentProvider()):
            self.assertIsNone(provider.credential_refresh_spec(cast(Any, plan)))


if __name__ == "__main__":
    unittest.main()


class TestServiceAndJournal(unittest.TestCase):
    def test_the_service_asks_its_refresher(self) -> None:
        from bot_bottle.control.hostctl.bottles import BottleService  # pylint: disable=import-outside-toplevel
        service = SimpleNamespace(credentials=mock.Mock())
        service.credentials.refresh_now.return_value = {"error": "b1"}
        self.assertEqual({"error": "b1"},
                         BottleService.refresh_credentials(cast(Any, service), "b1"))

    def test_query_host_journal(self) -> None:
        from bot_bottle.control.hostctl.audit_index import query_host_journal  # pylint: disable=import-outside-toplevel
        with tempfile.TemporaryDirectory() as tmp:
            restore = use_bottle_root(Path(tmp))
            try:
                self.assertEqual([], query_host_journal("b1", None, 10))  # no journal yet
                from bot_bottle.paths import host_audit_dir  # pylint: disable=import-outside-toplevel
                host_audit_dir().mkdir(parents=True)
                self.assertEqual([], query_host_journal("b1", None, 10))
            finally:
                restore()
