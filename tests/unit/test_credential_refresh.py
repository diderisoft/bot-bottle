"""Keeping running bottles' forwarded host logins current."""

from __future__ import annotations

import json
import os
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
        self.timeouts: list[float | None] = []

    def exec(self, script: str, *, user: str = "node", timeout: float | None = None,
             private: bool = True) -> Any:
        del user, private
        self.timeouts.append(timeout)
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
        self.assertIsNotNone(bottle.timeouts[0])  # a wedged exec doesn't hold the refresher

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


class TestWatch(unittest.TestCase):
    """A rotated host login reaches the bottles at the next look at its file,
    not at the timer (the CLI revokes the old token as it rotates)."""

    CODEX = AdoptedCredentials("codex", (CODEX_HOST_CREDENTIAL_TOKEN_REF,),
                               slots={CODEX_HOST_CREDENTIAL_TOKEN_REF: ("EGRESS_TOKEN_0",)})

    def setUp(self) -> None:
        self.pushed: list[tuple[str, str, str, str]] = []
        self.audit = mock.Mock()
        self.provider = _Provider()
        self.enterContext(mock.patch("bot_bottle.agent_provider.get_provider",
                                     return_value=self.provider))
        def push(registry_id: str, slot: str, value: str, key: str) -> None:
            self.pushed.append((registry_id, slot, value, key))
        self.refresher = CredentialRefresher(push, audit=self.audit, clock=lambda: NOW)
        self.warn = self.enterContext(mock.patch.object(credential_refresh.log, "warn"))
        self.enterContext(mock.patch.object(credential_refresh.log, "info"))
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.claude_file = Path(tmp.name) / "claude.json"
        self.codex_file = Path(tmp.name) / "codex.json"
        files = {"claude": (self.claude_file,), "codex": (self.codex_file,), "pi": ()}
        self.stamp = 1_700_000_000_000_000_000
        def login_paths(harness: str) -> tuple[Path, ...]:
            return files[harness]
        self.refresher = CredentialRefresher(self.refresher._push, audit=self.audit,  # pylint: disable=protected-access
                                             clock=lambda: NOW, login_paths=login_paths)
        self.write(self.claude_file, "tok-0")
        self.claude_bottle, self.codex_bottle = _Bottle(), _Bottle()
        self.refresher.add("c1", CREDS, registry_id="r1", bottle=cast(Any, self.claude_bottle),
                           env_var_secret="key", current={REF: "tok-0"})

    def write(self, path: Path, token: str, *, partial: bool = False) -> None:
        text = json.dumps({"claudeAiOauth": {"accessToken": token}})
        path.write_text(text[:-3] if partial else text)
        # A distinct mtime even when written within the filesystem's resolution.
        self.stamp += 1_000_000_000
        os.utime(path, ns=(self.stamp, self.stamp))

    def test_the_first_look_only_notes_the_files(self) -> None:
        self.assertEqual([], self.refresher.watch_once())
        self.assertEqual([], self.pushed)

    def test_a_rotation_reaches_its_harness_bottles_at_once(self) -> None:
        self.refresher.add("x1", self.CODEX, registry_id="r2", bottle=cast(Any, self.codex_bottle),
                           env_var_secret="key", current={CODEX_HOST_CREDENTIAL_TOKEN_REF: "x"})
        self.write(self.codex_file, "x")
        self.refresher.watch_once()
        self.provider.result = CredentialRefresh({REF: "tok-1"}, NOW + timedelta(days=5))
        self.write(self.claude_file, "tok-1")
        self.assertEqual(["c1"], self.refresher.watch_once())
        self.assertEqual([("r1", "EGRESS_TOKEN_0", "tok-1", "key"),
                          ("r1", "EGRESS_TOKEN_1", "tok-1", "key")], self.pushed)
        self.audit.credential_event.assert_called_once()
        self.assertEqual("credential.renewed", self.audit.credential_event.call_args.args[0])
        # Nothing new: nothing more.
        self.assertEqual([], self.refresher.watch_once())

    def test_a_half_written_file_waits_for_the_next_look(self) -> None:
        self.refresher.watch_once()
        self.write(self.claude_file, "tok-1", partial=True)
        self.assertEqual([], self.refresher.watch_once())
        self.assertEqual([], self.pushed)
        self.provider.result = CredentialRefresh({REF: "tok-1"}, NOW + timedelta(days=5))
        self.write(self.claude_file, "tok-1")
        self.assertEqual(["c1"], self.refresher.watch_once())
        self.assertEqual(2, len(self.pushed))

    def test_an_unchanged_token_isnt_pushed(self) -> None:
        # The CLI rewrote the file (a new expiry, say) but kept the token.
        self.refresher.watch_once()
        self.provider.result = CredentialRefresh({REF: "tok-0"}, NOW + timedelta(days=6))
        self.write(self.claude_file, "tok-0")
        self.refresher.watch_once()
        self.assertEqual([], self.pushed)

    def test_a_harness_without_login_files_is_left_to_the_timer(self) -> None:
        pi = AdoptedCredentials("pi", ("PI_REF",), slots={"PI_REF": ("EGRESS_TOKEN_0",)})
        self.refresher.remove("c1")
        self.refresher.add("p1", pi, registry_id="r3", bottle=cast(Any, _Bottle()))
        self.assertEqual([], self.refresher.watch_once())
        self.assertEqual([], self.pushed)

    def test_the_providers_name_their_login_files(self) -> None:
        env = {"HOME": "/h", "CODEX_HOME": "/c"}
        self.assertEqual((Path("/h/.claude/.credentials.json"),),
                         ClaudeAgentProvider().host_login_paths(env))
        self.assertEqual((Path("/c/auth.json"),), CodexAgentProvider().host_login_paths(env))


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


class TestWatchEdges(TestWatch):
    """What the watch does around the edges: a missing file, a bottle whose
    refresh fails, the provider's own paths, and the thread itself."""

    def test_a_missing_file_is_noted_not_an_error(self) -> None:
        self.claude_file.unlink()
        self.assertEqual([], self.refresher.watch_once())  # noted as absent
        self.provider.result = CredentialRefresh({REF: "tok-1"}, NOW + timedelta(days=5))
        self.write(self.claude_file, "tok-1")  # it appears: a new login
        self.assertEqual(["c1"], self.refresher.watch_once())

    def test_one_bottles_failure_doesnt_stop_the_rest(self) -> None:
        self.refresher.add("c2", CREDS, registry_id="r2", bottle=cast(Any, _Bottle()),
                           env_var_secret="key", current={REF: "tok-0"})
        self.refresher.watch_once()
        self.write(self.claude_file, "tok-1")
        real = self.refresher.refresh
        def refresh(target: Any) -> Any:
            if target.identity == "c1":
                raise RuntimeError("host unreachable")
            return real(target)
        self.provider.result = CredentialRefresh({REF: "tok-1"}, NOW + timedelta(days=5))
        with mock.patch.object(self.refresher, "refresh", side_effect=refresh):
            self.assertEqual(["c2"], self.refresher.watch_once())
        warned = " ".join(str(c.args[0]) for c in self.warn.call_args_list)
        self.assertIn("host unreachable", warned)

    def test_the_watch_thread_looks_and_survives_an_error(self) -> None:
        import threading
        looked = threading.Event()
        calls: list[int] = []
        def watch_once() -> list[str]:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            looked.set()
            return []
        with mock.patch.object(credential_refresh, "WATCH_SECONDS", 0.01), \
             mock.patch.object(credential_refresh, "REFRESH_SECONDS", 3600), \
             mock.patch.object(self.refresher, "watch_once", side_effect=watch_once):
            self.refresher.start()
            self.assertTrue(looked.wait(2))
            self.refresher.stop()
        warned = " ".join(str(c.args[0]) for c in self.warn.call_args_list)
        self.assertIn("watching the host logins failed: boom", warned)


class TestLoginPaths(unittest.TestCase):
    def test_a_provider_names_none_by_default(self) -> None:
        from bot_bottle.agent_provider import AgentProvider
        self.assertEqual((), AgentProvider.host_login_paths(cast(Any, object()), {}))

    def test_the_watch_asks_the_harness_provider(self) -> None:
        provider = mock.Mock()
        provider.host_login_paths.return_value = (Path("/x/login.json"),)
        with mock.patch("bot_bottle.agent_provider.get_provider", return_value=provider):
            self.assertEqual((Path("/x/login.json"),),
                             credential_refresh._login_paths("claude"))  # pylint: disable=protected-access
