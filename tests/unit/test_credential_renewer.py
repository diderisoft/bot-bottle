"""Tests for CredentialRenewer and _HostLoginProvider."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

from bot_bottle.credentials import (
    CredentialProvider, Grant, KIND_TOKEN, MintedCredential,
)
from bot_bottle.control.hostctl.credential_renewer import (
    RENEW_BEFORE_FRACTION, MIN_RENEW_BEFORE,
    CredentialRenewer, _CredEntry, _renew_before,
)


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
EXPIRES_1H = NOW + timedelta(hours=1)
EXPIRES_24H = NOW + timedelta(hours=24)


def _minted(token: str = "tok-0", expires_at: datetime | None = EXPIRES_1H,
            kind: str = KIND_TOKEN) -> MintedCredential:
    return MintedCredential(kind, {"token": token}, expires_at, "test")


class _FakeProvider(CredentialProvider):
    source = "fake"
    kinds = (KIND_TOKEN,)
    project_definable = True

    def __init__(self, tokens: list[str] | None = None,
                 fail_renew: bool = False) -> None:
        self._tokens = iter(tokens or [])
        self._fail_renew = fail_renew
        self.mint_calls = 0
        self.renew_calls = 0
        self.retire_calls = 0

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: object) -> MintedCredential:
        self.mint_calls += 1
        try:
            tok = next(self._tokens)
        except StopIteration:
            tok = "tok-fallback"
        expires_at = settings.get("expires_at")
        assert expires_at is None or isinstance(expires_at, datetime)
        return MintedCredential(KIND_TOKEN, {"token": tok}, expires_at, "fake")

    def renew(self, label: str, bottle_id: str, current: MintedCredential,
              settings: Mapping[str, object], secrets: object) -> MintedCredential:
        self.renew_calls += 1
        if self._fail_renew:
            raise RuntimeError("simulated renew failure")
        try:
            tok = next(self._tokens)
        except StopIteration:
            tok = "tok-renewed"
        expires_at = settings.get("expires_at")
        assert expires_at is None or isinstance(expires_at, datetime)
        return MintedCredential(KIND_TOKEN, {"token": tok}, expires_at, "fake")

    def retire(self, label: str, bottle_id: str, current: MintedCredential) -> None:
        self.retire_calls += 1

    def describe(self, settings: Mapping[str, object]) -> Grant:
        return Grant("", "", "", "", "fake")


def _make_renewer(**kw: Any) -> tuple[CredentialRenewer, list[tuple[Any, ...]]]:
    pushed: list[tuple[Any, ...]] = []

    def push(*a: Any) -> None:
        pushed.append(a)

    kw.setdefault("clock", lambda: NOW)
    return CredentialRenewer(push, **kw), pushed


def _recorder(calls: list[tuple[Any, ...]]) -> Callable[..., None]:
    """A callback that records each call's arguments in `calls`."""
    def record(*a: Any) -> None:
        calls.append(a)
    return record


# ── _renew_before ──────────────────────────────────────────────────────────────

class TestRenewBefore(unittest.TestCase):

    def test_short_lifetime_uses_min(self) -> None:
        # 20 min lifetime → 20% = 4 min, but MIN_RENEW_BEFORE = 10 min wins.
        issued = NOW
        expires = NOW + timedelta(minutes=20)
        result = _renew_before(issued, expires)
        self.assertEqual(result, MIN_RENEW_BEFORE)

    def test_long_lifetime_uses_fraction(self) -> None:
        # 100 min lifetime → 20% = 20 min > MIN_RENEW_BEFORE(10 min).
        issued = NOW
        expires = NOW + timedelta(minutes=100)
        result = _renew_before(issued, expires)
        self.assertEqual(result, timedelta(minutes=100) * RENEW_BEFORE_FRACTION)

    def test_exactly_at_min(self) -> None:
        # 50 min lifetime → 20% = 10 min = MIN_RENEW_BEFORE exactly.
        issued = NOW
        expires = NOW + timedelta(minutes=50)
        result = _renew_before(issued, expires)
        self.assertEqual(result, MIN_RENEW_BEFORE)


# ── should_renew ───────────────────────────────────────────────────────────────

class TestShouldRenew(unittest.TestCase):

    def _entry(self, issued_at: datetime, expires_at: datetime | None,
               provider: _FakeProvider | None = None) -> _CredEntry:
        cred = MintedCredential(KIND_TOKEN, {"token": "t"}, expires_at, "fake")
        return _CredEntry(
            label="x", cred=cred, issued_at=issued_at,
            provider=provider or _FakeProvider(),
            settings={}, secrets=None, slots={},  # type: ignore[arg-type]
        )

    def test_no_expiry_never_renews(self) -> None:
        entry = self._entry(NOW, None)
        self.assertFalse(entry.should_renew(NOW))

    def test_far_future_no_renew(self) -> None:
        issued = NOW
        expires = NOW + timedelta(hours=24)
        entry = self._entry(issued, expires)
        # 10 min before expiry is the renew window; 23h55m remains → no
        self.assertFalse(entry.should_renew(NOW))

    def test_within_window_renews(self) -> None:
        issued = NOW - timedelta(hours=23)
        expires = NOW + timedelta(minutes=5)
        entry = self._entry(issued, expires)
        self.assertTrue(entry.should_renew(NOW))

    def test_just_at_boundary(self) -> None:
        # exactly at the renew-before window → should renew
        issued = NOW - timedelta(hours=50)
        expires = NOW + MIN_RENEW_BEFORE
        entry = self._entry(issued, expires)
        self.assertTrue(entry.should_renew(NOW))


# ── CredentialRenewer.add / remove ─────────────────────────────────────────────

class TestCredentialRenewerAddRemove(unittest.TestCase):

    def test_add_then_remove(self) -> None:
        renewer, _ = _make_renewer()
        prov = _FakeProvider()
        renewer.add("b1", "reg1", "evsecret", [
            ("my-cred", _minted(), prov, {}, None, {}),  # type: ignore[arg-type]
        ])
        self.assertIn("b1", renewer._bottles)
        renewer.remove("b1")
        self.assertNotIn("b1", renewer._bottles)

    def test_remove_nonexistent_ok(self) -> None:
        renewer, _ = _make_renewer()
        renewer.remove("not-there")  # must not raise

    def test_add_overwrites_existing(self) -> None:
        renewer, _ = _make_renewer()
        prov = _FakeProvider()
        renewer.add("b1", "reg1", "ev1", [("c1", _minted(), prov, {}, None, {})])  # type: ignore[arg-type]
        renewer.add("b1", "reg2", "ev2", [("c2", _minted(), prov, {}, None, {})])  # type: ignore[arg-type]
        entry = renewer._bottles["b1"]
        self.assertEqual(entry.registry_id, "reg2")
        self.assertIn("c2", entry.creds)
        self.assertNotIn("c1", entry.creds)


# ── CredentialRenewer.renew_all ────────────────────────────────────────────────

class TestCredentialRenewerRenewAll(unittest.TestCase):

    def _near_expiry_minted(self, tok: str = "old") -> MintedCredential:
        # expires in 2 min from NOW → within MIN_RENEW_BEFORE(10 min)
        return MintedCredential(KIND_TOKEN, {"token": tok},
                                NOW + timedelta(minutes=2), "fake")

    def test_renew_pushed_to_slot(self) -> None:
        renewer, pushed = _make_renewer()
        prov = _FakeProvider(tokens=["new-tok"])
        slots = {"token": ("EGRESS_TOKEN_0",)}
        renewer.add("b1", "reg1", "evsec",
                    [("tk", self._near_expiry_minted(), prov, {}, None, slots)])  # type: ignore[arg-type]
        renewer.renew_all()
        self.assertEqual(len(pushed), 1)
        reg, slot, val, _evsec = pushed[0]
        self.assertEqual(slot, "EGRESS_TOKEN_0")
        self.assertEqual(val, "new-tok")
        self.assertEqual(reg, "reg1")

    def test_no_renew_when_not_expiring(self) -> None:
        renewer, pushed = _make_renewer()
        prov = _FakeProvider(tokens=["new-tok"])
        # expires in 6 hours → well outside renew window
        minted = MintedCredential(KIND_TOKEN, {"token": "orig"},
                                  NOW + timedelta(hours=6), "fake")
        renewer.add("b1", "reg1", "evsec",
                    [("tk", minted, prov, {}, None, {"token": ("SLOT0",)})])  # type: ignore[arg-type]
        renewer.renew_all()
        self.assertEqual(pushed, [])
        self.assertEqual(prov.renew_calls, 0)

    def test_renew_failure_does_not_stop_others(self) -> None:
        renewer, pushed = _make_renewer()
        failing_prov = _FakeProvider(fail_renew=True)
        ok_prov = _FakeProvider(tokens=["tok2"])
        near = self._near_expiry_minted()
        renewer.add("b1", "reg1", "evsec", [
            ("bad", near, failing_prov, {}, None, {"token": ("SLOT0",)}),  # type: ignore[arg-type]
            ("good", near, ok_prov, {}, None, {"token": ("SLOT1",)}),  # type: ignore[arg-type]
        ])
        renewer.renew_all()
        # good credential was pushed
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][1], "SLOT1")

    def test_no_push_when_value_unchanged(self) -> None:
        renewer, pushed = _make_renewer()
        prov = _FakeProvider(tokens=["same"])
        # issuing same token → no push
        near = MintedCredential(KIND_TOKEN, {"token": "same"},
                                NOW + timedelta(minutes=2), "fake")
        renewer.add("b1", "reg1", "evsec",
                    [("tk", near, prov, {}, None, {"token": ("SLOT0",)})])  # type: ignore[arg-type]
        renewer.renew_all()
        self.assertEqual(pushed, [])

    def test_multiple_slots_per_field(self) -> None:
        renewer, pushed = _make_renewer()
        prov = _FakeProvider(tokens=["t2"])
        near = self._near_expiry_minted("t1")
        slots = {"token": ("SLOT0", "SLOT1", "SLOT2")}
        renewer.add("b1", "reg1", "evsec",
                    [("tk", near, prov, {}, None, slots)])  # type: ignore[arg-type]
        renewer.renew_all()
        self.assertEqual(len(pushed), 3)
        slot_names = {p[1] for p in pushed}
        self.assertEqual(slot_names, {"SLOT0", "SLOT1", "SLOT2"})

    def test_audit_renewed_emitted(self) -> None:
        events: list[tuple[Any, ...]] = []

        class _Audit:
            def credential_event(self, kind: str, **kw: object) -> None:
                events.append((kind, kw))

        renewer, _ = _make_renewer(audit=_Audit())
        prov = _FakeProvider(tokens=["new"])
        near = self._near_expiry_minted()
        renewer.add("b1", "reg1", "evsec",
                    [("tk", near, prov, {}, None, {"token": ("SLOT0",)})])  # type: ignore[arg-type]
        renewer.renew_all()
        kinds = [e[0] for e in events]
        self.assertIn("credential.renewed", kinds)

    def test_audit_renew_failed_emitted(self) -> None:
        events: list[tuple[Any, ...]] = []

        class _Audit:
            def credential_event(self, kind: str, **kw: object) -> None:
                events.append((kind, kw))

        renewer, _ = _make_renewer(audit=_Audit())
        prov = _FakeProvider(fail_renew=True)
        near = self._near_expiry_minted()
        renewer.add("b1", "reg1", "evsec",
                    [("tk", near, prov, {}, None, {})])  # type: ignore[arg-type]
        renewer.renew_all()
        kinds = [e[0] for e in events]
        self.assertIn("credential.renew_failed", kinds)


# ── CredentialRenewer.retire ───────────────────────────────────────────────────

class TestCredentialRenewerRetire(unittest.TestCase):

    def test_retire_calls_provider_retire(self) -> None:
        renewer, _ = _make_renewer()
        prov = _FakeProvider()
        renewer.add("b1", "reg1", "evsec",
                    [("tk", _minted(), prov, {}, None, {})])  # type: ignore[arg-type]
        renewer.retire("b1")
        self.assertEqual(prov.retire_calls, 1)
        self.assertNotIn("b1", renewer._bottles)

    def test_retire_nonexistent_ok(self) -> None:
        renewer, _ = _make_renewer()
        renewer.retire("not-there")  # must not raise

    def test_retire_emits_audit(self) -> None:
        events: list[str] = []

        class _Audit:
            def credential_event(self, kind: str, **kw: object) -> None:
                events.append(kind)

        renewer, _ = _make_renewer(audit=_Audit())
        prov = _FakeProvider()
        renewer.add("b1", "reg1", "evsec",
                    [("tk", _minted(), prov, {}, None, {})])  # type: ignore[arg-type]
        renewer.retire("b1")
        self.assertIn("credential.retired", events)

    def test_retire_provider_error_continues(self) -> None:
        def _bad_retire(*a: object) -> None:
            raise RuntimeError("boom")

        renewer, _ = _make_renewer()
        prov1 = _FakeProvider()
        prov1.retire = _bad_retire  # type: ignore[method-assign]
        prov2 = _FakeProvider()
        renewer.add("b1", "reg1", "evsec", [
            ("bad", _minted(), prov1, {}, None, {}),  # type: ignore[arg-type]
            ("good", _minted(), prov2, {}, None, {}),  # type: ignore[arg-type]
        ])
        renewer.retire("b1")  # must not raise
        self.assertEqual(prov2.retire_calls, 1)


# ── CredentialRenewer.start / stop ─────────────────────────────────────────────

class TestCredentialRenewerThread(unittest.TestCase):

    def test_start_stop(self) -> None:
        renewer, _ = _make_renewer()
        renewer.start()
        # Thread is daemon — just verify stop doesn't deadlock.
        renewer.stop()


# ── _HostLoginProvider ─────────────────────────────────────────────────────────

class TestHostLoginProvider(unittest.TestCase):

    def _get_provider(self) -> CredentialProvider:
        from bot_bottle.credentials import _HostLoginProvider  # type: ignore[attr-defined]
        return _HostLoginProvider()

    def test_mint_missing_harness_raises(self) -> None:
        p = self._get_provider()
        with self.assertRaises(ValueError, msg="requires 'harness'"):
            p.mint("my-cred", "b1", {}, None)  # type: ignore[arg-type]

    def test_mint_unknown_harness_raises(self) -> None:
        p = self._get_provider()
        with self.assertRaises(ValueError):
            p.mint("my-cred", "b1", {"harness": "no-such-harness"}, None)  # type: ignore[arg-type]

    def test_mint_calls_read_host_login(self) -> None:
        p = self._get_provider()
        expires = NOW + timedelta(hours=1)
        fake_provider = mock.MagicMock()
        fake_provider.read_host_login.return_value = ("my-token", expires)

        with mock.patch("bot_bottle.agent_provider.get_provider", return_value=fake_provider):
            result = p.mint("my-cred", "b1", {"harness": "claude"}, None)  # type: ignore[arg-type]

        self.assertEqual(result.kind, KIND_TOKEN)
        self.assertEqual(result.values["token"], "my-token")
        self.assertEqual(result.expires_at, expires)
        self.assertEqual(result.principal, "host_login:claude")

    def test_mint_host_login_error_reraises(self) -> None:
        from bot_bottle.errors import HostLoginError
        p = self._get_provider()
        fake_provider = mock.MagicMock()
        fake_provider.read_host_login.side_effect = HostLoginError("not logged in")

        with mock.patch("bot_bottle.agent_provider.get_provider", return_value=fake_provider):
            with self.assertRaises(ValueError, msg="host login"):
                p.mint("my-cred", "b1", {"harness": "claude"}, None)  # type: ignore[arg-type]

    def test_mint_not_implemented_reraises(self) -> None:
        p = self._get_provider()
        fake_provider = mock.MagicMock()
        fake_provider.read_host_login.side_effect = NotImplementedError("no host login")

        with mock.patch("bot_bottle.agent_provider.get_provider", return_value=fake_provider):
            with self.assertRaises(ValueError, msg="host login"):
                p.mint("my-cred", "b1", {"harness": "some-harness"}, None)  # type: ignore[arg-type]

    def test_describe_no_harness(self) -> None:
        p = self._get_provider()
        g = p.describe({})
        self.assertIn("host login", g.starts_from)
        self.assertIn("until", g.lifetime)

    def test_describe_with_harness(self) -> None:
        p = self._get_provider()
        g = p.describe({"harness": "codex"})
        self.assertIn("codex", g.starts_from)

    def test_source_and_kinds(self) -> None:
        p = self._get_provider()
        self.assertEqual(p.source, "host_login")
        self.assertEqual(p.kinds, (KIND_TOKEN,))
        self.assertFalse(p.project_definable)

    def test_in_registry(self) -> None:
        from bot_bottle.credentials import CredentialRegistry
        reg = CredentialRegistry(contrib_root=None)
        prov = reg.get("host_login")
        self.assertIsNotNone(prov)
        self.assertEqual(prov.source, "host_login")  # type: ignore[union-attr]


# ── readable_push on renewal ───────────────────────────────────────────────────

class TestReadablePush(unittest.TestCase):
    """CredentialRenewer calls readable_push when a readable credential renews."""

    def _near_expiry(self) -> MintedCredential:
        return MintedCredential(KIND_TOKEN, {"token": "old-tok"},
                                NOW + timedelta(minutes=2), "fake")

    def test_readable_push_called_on_renewal(self) -> None:
        readable_calls: list[tuple[Any, ...]] = []
        renewer, _ = _make_renewer(readable_push=_recorder(readable_calls))
        prov = _FakeProvider(tokens=["new-tok"])
        minted = self._near_expiry()
        renewer.add("b1", "reg1", "evsec",
                    [("lbl", minted, prov, {}, None, {})],  # type: ignore[arg-type]
                    readable_labels=frozenset({"lbl"}))
        renewer.renew_all()
        self.assertEqual(len(readable_calls), 1)
        bottle_id, label, values = readable_calls[0]
        self.assertEqual(bottle_id, "b1")
        self.assertEqual(label, "lbl")
        self.assertEqual(values.get("token"), "new-tok")

    def test_readable_push_not_called_when_not_readable(self) -> None:
        readable_calls: list[tuple[Any, ...]] = []
        renewer, _ = _make_renewer(readable_push=_recorder(readable_calls))
        prov = _FakeProvider(tokens=["new-tok"])
        minted = self._near_expiry()
        renewer.add("b1", "reg1", "evsec",
                    [("lbl", minted, prov, {}, None, {})],  # type: ignore[arg-type]
                    readable_labels=frozenset())  # not readable
        renewer.renew_all()
        self.assertEqual(readable_calls, [])

    def test_readable_push_not_called_when_no_callback(self) -> None:
        renewer, _ = _make_renewer()  # no readable_push
        prov = _FakeProvider(tokens=["new-tok"])
        minted = self._near_expiry()
        renewer.add("b1", "reg1", "evsec",
                    [("lbl", minted, prov, {}, None, {})],  # type: ignore[arg-type]
                    readable_labels=frozenset({"lbl"}))
        # Should not raise even with no callback.
        renewer.renew_all()

    def test_readable_push_error_does_not_stop_renewal(self) -> None:
        def bad_push(*_: object) -> None:
            raise RuntimeError("push failed")

        renewer, pushed = _make_renewer(readable_push=bad_push)
        prov = _FakeProvider(tokens=["new-tok"])
        minted = self._near_expiry()
        renewer.add("b1", "reg1", "evsec",
                    [("lbl", minted, prov, {}, None, {"token": ("SLOT0",)})],  # type: ignore[arg-type]
                    readable_labels=frozenset({"lbl"}))
        # Renew should complete; the egress push still happens.
        renewer.renew_all()
        # egress push still happened
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][1], "SLOT0")

    def test_unchanged_value_does_not_trigger_readable_push(self) -> None:
        readable_calls: list[tuple[Any, ...]] = []
        renewer, _ = _make_renewer(readable_push=_recorder(readable_calls))
        prov = _FakeProvider(tokens=["same-tok"])
        minted = MintedCredential(KIND_TOKEN, {"token": "same-tok"},
                                  NOW + timedelta(minutes=2), "fake")
        renewer.add("b1", "reg1", "evsec",
                    [("lbl", minted, prov, {}, None, {})],  # type: ignore[arg-type]
                    readable_labels=frozenset({"lbl"}))
        renewer.renew_all()
        # No change → no push.
        self.assertEqual(readable_calls, [])


class TestStatusFor(unittest.TestCase):
    def test_empty_for_unknown_bottle(self) -> None:
        renewer, _ = _make_renewer()
        self.assertEqual([], renewer.status_for("no-such-bottle"))

    def test_returns_metadata_no_values(self) -> None:
        from bot_bottle.credentials import KIND_AWS, MintedCredential as MC
        minted = MC(KIND_AWS, {"access_key_id": "AKIA…", "secret_access_key": "SECRET"},
                    EXPIRES_1H, "arn:aws:iam::123:role/r")
        renewer, _ = _make_renewer()
        renewer.add("b1", "reg1", "evsec",
                    [("aws-cred", minted, _FakeProvider(), {}, None, {})],  # type: ignore[arg-type]
                    readable_labels=frozenset())
        result = renewer.status_for("b1")
        self.assertEqual(1, len(result))
        item = result[0]
        self.assertEqual("aws-cred", item["label"])
        self.assertEqual(KIND_AWS, item["kind"])
        self.assertEqual("arn:aws:iam::123:role/r", item["principal"])
        self.assertEqual(EXPIRES_1H.isoformat(), item["expires_at"])
        self.assertFalse(item["agent_can_read"])
        # Values must not be present.
        self.assertNotIn("access_key_id", item)
        self.assertNotIn("secret_access_key", item)

    def test_agent_can_read_flag(self) -> None:
        minted = _minted(expires_at=None)
        renewer, _ = _make_renewer()
        renewer.add("b2", "reg2", "evsec",
                    [("tok", minted, _FakeProvider(), {}, None, {})],  # type: ignore[arg-type]
                    readable_labels=frozenset({"tok"}))
        result = renewer.status_for("b2")
        self.assertTrue(result[0]["agent_can_read"])
        self.assertIsNone(result[0]["expires_at"])

    def test_multiple_credentials(self) -> None:
        from bot_bottle.credentials import KIND_AWS, MintedCredential as MC
        c1 = _minted("tok-a")
        c2 = MC(KIND_AWS, {"access_key_id": "X", "secret_access_key": "Y"}, None, "role-b")
        renewer, _ = _make_renewer()
        renewer.add("b3", "reg3", "evsec",
                    [("alpha", c1, _FakeProvider(), {}, None, {}),   # type: ignore[arg-type]
                     ("beta", c2, _FakeProvider(), {}, None, {})],   # type: ignore[arg-type]
                    readable_labels=frozenset())
        result = renewer.status_for("b3")
        labels = {r["label"] for r in result}
        self.assertEqual({"alpha", "beta"}, labels)


if __name__ == "__main__":
    unittest.main()
