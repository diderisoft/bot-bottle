"""Audit records on the host (ADR 0013): passkey assertions verified here,
the per-session unlock, encrypted pushes that never repeat, and reads only
after an unlock."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from bot_bottle import paths
from bot_bottle.cli.commands import audit_passkeys
from bot_bottle.control.hostctl import audit_trust
from bot_bottle.control.hostctl.audit_link import AuditLink
from bot_bottle.control.hostctl.audit_push import (
    BATCH, AuditPusher, decrypt, encrypt, host_runs, load_key, load_state, record_id,
    record_type,
)
from bot_bottle.control.hostctl.audit_unlock import (
    READS_PER_MINUTE, TOKEN_IDLE_SECONDS, TOKEN_MAX_SECONDS, AuditUnlock, UnlockError,
)
from bot_bottle.control.hostctl.webauthn import (
    WebAuthnError, b64url_encode, cose_key, fingerprint, key_alg, verify_assertion,
)

RP = "console.example"
ORIGIN = "https://console.example"


# --- a passkey, made here ---

def _cbor(v: Any) -> bytes:
    def head(major: int, n: int) -> bytes:
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
        raise ValueError(n)
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    assert isinstance(v, dict)
    return head(5, len(v)) + b"".join(_cbor(k) + _cbor(x) for k, x in v.items())


class Passkey:
    def __init__(self, kind: str = "es256", cred_id: str = "cred-1") -> None:
        self.id = cred_id
        self.kind = kind
        if kind == "es256":
            self._key: Any = ec.generate_private_key(ec.SECP256R1())
            n = self._key.public_key().public_numbers()
            self.cose = _cbor({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"),
                               -3: n.y.to_bytes(32, "big")})
        else:
            self._key = ed25519.Ed25519PrivateKey.generate()
            raw = self._key.public_key().public_bytes_raw()
            self.cose = _cbor({1: 1, 3: -8, -1: 6, -2: raw})
        self.counter = 0

    def assertion(self, challenge: str, *, rp: str = RP, origin: str = ORIGIN,
                  flags: int = 0x05, counter: int | None = None, typ: str = "webauthn.get",
                  cross: bool = False, cred_id: str | None = None,
                  tamper: bool = False) -> dict[str, Any]:
        self.counter += 1
        c = self.counter if counter is None else counter
        auth = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + c.to_bytes(4, "big")
        client = json.dumps({"type": typ, "challenge": challenge, "origin": origin,
                             "crossOrigin": cross}).encode()
        signed = auth + hashlib.sha256(client).digest()
        if self.kind == "es256":
            sig = self._key.sign(signed, ec.ECDSA(hashes.SHA256()))
        else:
            sig = self._key.sign(signed)
        if tamper:
            sig = bytes([sig[0] ^ 1]) + sig[1:]
        return {"id": cred_id or self.id, "response": {
            "authenticatorData": b64url_encode(auth), "clientDataJSON": b64url_encode(client),
            "signature": b64url_encode(sig)}}


def _verify(pk: Passkey, assertion: dict[str, Any], challenge: bytes, stored: int = 0) -> Any:
    return verify_assertion(assertion, public_key=pk.cose, credential_id=pk.id,
                            challenge=challenge, rp_id=RP, origin=ORIGIN,
                            stored_sign_count=stored)


class TestVerifyAssertion(unittest.TestCase):
    def setUp(self) -> None:
        self.ch = os.urandom(32)
        self.c = b64url_encode(self.ch)

    def test_es256_and_eddsa_verify(self) -> None:
        for kind in ("es256", "eddsa"):
            with self.subTest(kind=kind):
                pk = Passkey(kind)
                got = _verify(pk, pk.assertion(self.c), self.ch)
                self.assertEqual(1, got.sign_count)
                self.assertTrue(got.user_verified)
                self.assertEqual(-7 if kind == "es256" else -8, key_alg(pk.cose))

    def test_refusals(self) -> None:
        pk = Passkey()
        cases = {
            "other challenge": (pk.assertion(b64url_encode(os.urandom(32))), 0),
            "other site": (pk.assertion(self.c, rp="evil.example"), 0),
            "other origin": (pk.assertion(self.c, origin="https://evil.example"), 0),
            "cross origin": (pk.assertion(self.c, cross=True), 0),
            "registration": (pk.assertion(self.c, typ="webauthn.create"), 0),
            "not present": (pk.assertion(self.c, flags=0x00), 0),
            "counter back": (pk.assertion(self.c, counter=3), 5),
            "bad signature": (pk.assertion(self.c, tamper=True), 0),
            "other passkey": (pk.assertion(self.c, cred_id="cred-2"), 0),
        }
        for name, (assertion, stored) in cases.items():
            with self.subTest(name), self.assertRaises(WebAuthnError):
                _verify(pk, assertion, self.ch, stored)

    def test_malformed(self) -> None:
        pk = Passkey()
        for bad in ({}, {"id": pk.id}, {"id": pk.id, "response": {"authenticatorData": "x"}},
                    {"id": pk.id, "response": {"authenticatorData": "", "clientDataJSON":
                                               b64url_encode(b"not json"), "signature": ""}}):
            with self.subTest(bad=bad), self.assertRaises(WebAuthnError):
                _verify(pk, bad, self.ch)
        with self.assertRaises(WebAuthnError):
            key_alg(_cbor({1: 2, 3: -257}))
        with self.assertRaises(WebAuthnError):
            cose_key(b"\x18")  # truncated

    def test_user_verification_can_be_required(self) -> None:
        pk = Passkey()
        with self.assertRaises(WebAuthnError):
            verify_assertion(pk.assertion(self.c, flags=0x01), public_key=pk.cose,
                             credential_id=pk.id, challenge=self.ch, rp_id=RP, origin=ORIGIN,
                             stored_sign_count=0, require_uv=True)

    def test_fingerprint_is_grouped_hex(self) -> None:
        fp = fingerprint(b"key")
        self.assertEqual(8, len(fp.split()))
        self.assertEqual(hashlib.sha256(b"key").hexdigest()[:32], fp.replace(" ", ""))


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.dir = Path(self._tmp.name)
        self.trust_path = self.dir / "audit-passkeys.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def trust(self, *pks: Passkey) -> None:
        audit_trust.save(audit_trust.Trust(origin=ORIGIN, rp_id=RP, passkeys=[
            audit_trust.TrustedPasskey(id=p.id, public_key=b64url_encode(p.cose)) for p in pks]),
            self.trust_path)


class TestUnlock(_Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.now = 1000.0
        self.unlock = AuditUnlock(self.trust_path, clock=lambda: self.now)
        self.pk = Passkey()
        self.trust(self.pk)

    def _unlock(self, session: str = "s1") -> str:
        ch = self.unlock.challenge(session)
        return str(self.unlock.unlock(session, self.pk.assertion(str(ch["challenge"])))["token"])

    def test_no_trusted_passkey(self) -> None:
        self.trust_path.unlink()
        with self.assertRaisesRegex(UnlockError, "trusts no passkey"):
            self.unlock.challenge("s1")
        with self.assertRaises(UnlockError):
            self.unlock.challenge("")

    def test_challenge_offers_the_trusted_passkeys(self) -> None:
        ch = self.unlock.challenge("s1")
        self.assertEqual(RP, ch["rp_id"])
        self.assertEqual([self.pk.id], ch["credential_ids"])

    def test_unlock_then_read(self) -> None:
        token = self._unlock()
        self.assertEqual(self.pk.id, self.unlock.check(token, "s1"))
        # the counter is kept, so the same assertion's counter can't be reused
        self.assertEqual(1, audit_trust.load(self.trust_path).passkeys[0].sign_count)

    def test_a_challenge_is_single_use_and_bound_to_its_session(self) -> None:
        ch = str(self.unlock.challenge("s1")["challenge"])
        assertion = self.pk.assertion(ch)
        with self.assertRaisesRegex(UnlockError, "another session"):
            self.unlock.unlock("s2", assertion)
        with self.assertRaisesRegex(UnlockError, "expired or was used"):
            self.unlock.unlock("s1", assertion)  # used up by the failed try

    def test_an_expired_challenge(self) -> None:
        ch = str(self.unlock.challenge("s1")["challenge"])
        self.now += 121
        with self.assertRaisesRegex(UnlockError, "expired"):
            self.unlock.unlock("s1", self.pk.assertion(ch))

    def test_an_untrusted_or_bad_passkey(self) -> None:
        other = Passkey(cred_id="cred-x")
        ch = str(self.unlock.challenge("s1")["challenge"])
        with self.assertRaisesRegex(UnlockError, "isn't trusted"):
            self.unlock.unlock("s1", other.assertion(ch))
        ch = str(self.unlock.challenge("s1")["challenge"])
        with self.assertRaisesRegex(UnlockError, "signature"):
            self.unlock.unlock("s1", self.pk.assertion(ch, tamper=True))

    def test_token_bound_to_session_and_expires(self) -> None:
        token = self._unlock()
        with self.assertRaises(UnlockError):
            self.unlock.check(token, "s2")
        with self.assertRaises(UnlockError):
            self.unlock.check("nope", "s1")
        self.now += TOKEN_IDLE_SECONDS + 1
        with self.assertRaises(UnlockError):
            self.unlock.check(token, "s1")
        token = self._unlock()
        for _ in range(int(TOKEN_MAX_SECONDS // 1800) + 1):
            self.now += 1800
            try:
                self.unlock.check(token, "s1")
            except UnlockError:
                break
        else:
            self.fail("the token outlived its cap")

    def test_reads_are_rate_limited(self) -> None:
        token = self._unlock()
        self.unlock.check(token, "s1", READS_PER_MINUTE)
        with self.assertRaisesRegex(UnlockError, "too many"):
            self.unlock.check(token, "s1")
        self.now += 61
        self.unlock.check(token, "s1")


@dataclass
class _Entry:
    seq: int
    kind: str = "assistant"
    error: bool = False
    meta: bool = False
    ts: str = "2026-11-02T00:00:00Z"

    def to_json(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "text": f"entry {self.seq}"}


class TestPusher(_Tmp):
    KEY = bytes(range(32))

    def setUp(self) -> None:
        super().setUp()
        self.entries = [_Entry(i * 10) for i in range(BATCH + 5)] + [_Entry(999, meta=True)]
        self.sent: list[dict[str, Any]] = []

    async def _send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    def _entries(self, _agent_id: str) -> list[Any]:
        return list(self.entries)

    def pusher(self) -> AuditPusher:
        return AuditPusher("agent-1", self.KEY, self._entries, self._send,
                           directory=self.dir)

    def test_batches_and_never_repeats(self) -> None:
        p = self.pusher()
        self.assertEqual(BATCH, asyncio.run(p.push("run-a")))
        self.assertEqual(5, asyncio.run(p.push("run-a")))
        self.assertEqual(0, asyncio.run(p.push("run-a")))
        # a restarted host controller, and a resume (a new run), carry on
        again = self.pusher()
        self.assertEqual(0, asyncio.run(again.push("run-b")))
        self.entries.append(_Entry(5000, kind="user"))
        self.assertEqual(1, asyncio.run(again.push("run-b")))
        self.assertEqual(BATCH + 6, len(self.sent))
        self.assertEqual(BATCH + 6, len({m["id"] for m in self.sent}))
        self.assertEqual("user_message", self.sent[-1]["record_type"])

    def test_record_types_are_the_consoles(self) -> None:
        allowed = {"user_message", "assistant_message", "tool_call", "tool_result_meta",
                   "clear_event", "agent_error", "attention_event", "system_event"}
        for kind in ("user", "assistant", "tool_call", "tool_result", "system", "other"):
            with self.subTest(kind=kind):
                self.assertIn(record_type(_Entry(0, kind=kind)), allowed)  # type: ignore
        self.assertEqual("agent_error", record_type(_Entry(0, error=True)))  # type: ignore

    def test_the_chain_carries_on_across_runs(self) -> None:
        p = self.pusher()
        asyncio.run(p.push("run-a"))
        asyncio.run(p.push("run-a"))
        last_a = self.sent[-1]
        self.entries.append(_Entry(6000))
        asyncio.run(self.pusher().push("run-b"))
        first_b = self.sent[-1]
        self.assertEqual(last_a["chain_hash"], first_b["prev_chain_hash"])
        self.assertEqual(0, first_b["seq"])
        self.assertEqual(record_id("run-b", 0), first_b["id"])
        for prev, cur in zip(self.sent, self.sent[1:]):
            self.assertEqual(prev["chain_hash"], cur["prev_chain_hash"])
            self.assertEqual(hashlib.sha256((prev["chain_hash"] + cur["ciphertext"]).encode())
                             .hexdigest(), cur["chain_hash"])
        self.assertEqual({"run-a", "run-b"}, host_runs(self.dir))
        self.assertTrue(all("_entry_seq" not in m for m in self.sent))

    def test_ciphertext_is_bound_to_its_record(self) -> None:
        ct = encrypt(self.KEY, "run-a", 3, {"x": 1})
        self.assertEqual({"x": 1}, json.loads(decrypt(self.KEY, "run-a", 3, ct)))
        for run, seq in (("run-a", 4), ("run-b", 3)):
            with self.subTest(run=run, seq=seq), self.assertRaises(Exception):
                decrypt(self.KEY, run, seq, ct)

    def test_a_failed_send_resends_from_there(self) -> None:
        async def flaky(msg: dict[str, Any]) -> None:
            if len(self.sent) == 3:
                raise ConnectionError("dropped")
            self.sent.append(msg)
        p = AuditPusher("agent-1", self.KEY, self._entries, flaky, directory=self.dir)
        self.assertEqual(3, asyncio.run(p.push("run-a")))
        self.assertEqual(20, load_state(self.dir, "agent-1").last_entry_seq)
        self.sent.clear()
        p2 = self.pusher()
        asyncio.run(p2.push("run-a"))
        self.assertEqual(3, self.sent[0]["seq"])

    def test_unreadable_state_starts_over_and_no_run_sends_nothing(self) -> None:
        (self.dir / "agent-1.json").write_text("{not json")
        self.assertIsNone(load_state(self.dir, "agent-1").last_entry_seq)
        self.assertEqual(0, asyncio.run(self.pusher().push("")))

    def test_a_reader_error_is_retried_later(self) -> None:
        def boom(_a: str) -> list[Any]:
            raise OSError("gone")
        p = AuditPusher("agent-1", self.KEY, boom, self._send, directory=self.dir)
        self.assertEqual(0, asyncio.run(p.push("run-a")))


class _Bottles:
    def __init__(self, listed: list[dict[str, Any]]) -> None:
        self.listed = listed
        self.audit = mock.Mock()

    def list(self) -> list[dict[str, Any]]:
        return self.listed


class TestAuditLink(_Tmp):
    KEY = bytes(range(32))

    def setUp(self) -> None:
        super().setUp()
        self.now = 1000.0
        self.sent: list[dict[str, Any]] = []
        self.unlock = AuditUnlock(self.trust_path, clock=lambda: self.now)
        self.pk = Passkey()
        self.trust(self.pk)
        self.running = {"agent-1": "bottle-1"}
        self.bottles = _Bottles([{"id": "bottle-1", "activation": "run-a"}])
        self.link = AuditLink(self._send, lambda: self.running, self.bottles, unlock=self.unlock,
                              directory=self.dir, clock=lambda: self.now)
        self.link._key, self.link._key_tried = self.KEY, True  # pylint: disable=protected-access
        self.link._reader = mock.Mock(  # pylint: disable=protected-access
            entries=mock.Mock(return_value=[_Entry(1), _Entry(2)]))

    async def _send(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    def ask(self, msg: dict[str, Any]) -> dict[str, Any]:
        asyncio.run(self.link.handle(msg))
        return self.sent.pop()

    def test_push_is_throttled_and_only_for_running_agents(self) -> None:
        self.now += 10
        asyncio.run(self.link.push())
        self.assertEqual(2, len(self.sent))
        asyncio.run(self.link.push())  # within PUSH_SECONDS: nothing
        self.assertEqual(2, len(self.sent))
        self.running = {}
        self.now += 10
        asyncio.run(self.link.push())
        self.assertEqual({}, self.link._pushers)  # pylint: disable=protected-access

    def test_no_activation_or_list_error_sends_nothing(self) -> None:
        self.bottles.listed = [{"id": "bottle-1"}]
        self.now += 10
        asyncio.run(self.link.push())
        down = mock.Mock(side_effect=RuntimeError("down"))
        self.bottles.list = down  # type: ignore[method-assign]
        self.now += 10
        asyncio.run(self.link.push())
        self.assertEqual([], self.sent)

    def _token(self, session: str = "s1") -> str:
        ch = self.ask({"type": "audit_unlock_challenge", "request_id": "r1", "session": session})
        self.assertEqual("r1", ch["request_id"])
        got = self.ask({"type": "audit_unlock", "request_id": "r2", "session": session,
                        "assertion": self.pk.assertion(ch["challenge"])})
        self.assertEqual("audit_unlocked", got["type"])
        return got["token"]

    def test_unlock_then_decrypt_and_journal(self) -> None:
        self.now += 10
        asyncio.run(self.link.push())
        records = [{"id": m["id"], "seq": m["seq"], "ciphertext": m["ciphertext"]}
                   for m in self.sent]
        self.sent.clear()
        token = self._token()
        got = self.ask({"type": "decrypt_audit_records", "request_id": "r3", "session": "s1",
                        "token": token, "run_id": "run-a", "records": records})
        self.assertEqual("audit_decrypted", got["type"])
        self.assertEqual(["entry 1", "entry 2"],
                         [json.loads(r["plaintext"])["text"] for r in got["records"]])
        self.bottles.audit.audit_records_read.assert_called_once_with(
            run_id="run-a", count=2, credential_id=self.pk.id)

    def test_decrypt_refusals(self) -> None:
        self.now += 10
        asyncio.run(self.link.push())
        rec = {"id": self.sent[0]["id"], "seq": 0, "ciphertext": self.sent[0]["ciphertext"]}
        self.sent.clear()
        base = {"type": "decrypt_audit_records", "request_id": "r", "session": "s1",
                "run_id": "run-a", "records": [rec]}
        self.assertIn("passkey", self.ask({**base, "token": "nope"})["error"])
        token = self._token()
        self.assertIn("weren't sent by this host",
                      self.ask({**base, "token": token, "run_id": "run-z"})["error"])
        self.assertIn("records", self.ask({**base, "token": token, "records": []})["error"])
        other_session = self.ask({**base, "token": token, "session": "s2"})
        self.assertIn("passkey", other_session["error"])
        tampered = self.ask({**base, "token": token,
                             "records": [{**rec, "seq": 1}]})["records"][0]
        self.assertIn("doesn't decrypt", tampered["error"])
        self.link._key = None  # pylint: disable=protected-access
        self.assertIn("no audit key", self.ask({**base, "token": token})["error"])

    def test_unlock_needs_an_assertion(self) -> None:
        got = self.ask({"type": "audit_unlock", "request_id": "r", "session": "s1"})
        self.assertIn("assertion", got["error"])


class TestTrust(_Tmp):
    def test_save_load_and_counters(self) -> None:
        pk = Passkey()
        self.trust(pk)
        self.assertEqual(0o600, stat.S_IMODE(self.trust_path.stat().st_mode))
        audit_trust.record_sign_count(pk.id, 7, self.trust_path)
        audit_trust.record_sign_count(pk.id, 3, self.trust_path)
        self.assertEqual(7, audit_trust.load(self.trust_path).passkeys[0].sign_count)
        saved = audit_trust.load(self.trust_path).passkeys[0]
        self.assertEqual(fingerprint(pk.cose), saved.fingerprint)

    def test_load_errors(self) -> None:
        self.assertEqual([], audit_trust.load(self.trust_path).passkeys)
        self.trust_path.write_text("[1]")
        with self.assertRaises(audit_trust.TrustError):
            audit_trust.load(self.trust_path)
        self.trust_path.write_text("{bad")
        with self.assertRaises(audit_trust.TrustError):
            audit_trust.load(self.trust_path)

    def test_origin(self) -> None:
        self.assertEqual(("https://c.example:9444", "c.example"),
                         audit_trust.origin_and_rp_id("https://c.example:9444/x"))
        with self.assertRaises(audit_trust.TrustError):
            audit_trust.origin_and_rp_id("http://c.example")

    def test_fetch(self) -> None:
        good, eddsa = Passkey(), Passkey("eddsa", "cred-2")
        body = {"passkeys": [{"id": good.id, "public_key": b64url_encode(good.cose)},
                             {"id": eddsa.id, "public_key": b64url_encode(eddsa.cose)},
                             {"id": "x", "public_key": b64url_encode(_cbor({3: -257}))},
                             {"nope": 1}]}
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            got = audit_trust.fetch("https://c.example/", "tok")
        self.assertEqual([good.id, eddsa.id], [p.id for p in got])
        req = urlopen.call_args.args[0]
        self.assertEqual("https://c.example/api/v1/hosts/passkeys", req.full_url)
        self.assertEqual("Bearer tok", req.get_header("Authorization"))

    def test_fetch_errors(self) -> None:
        import urllib.error
        for err in (urllib.error.HTTPError("u", 401, "no", {}, None),  # type: ignore[arg-type]
                    urllib.error.URLError("down")):
            with self.subTest(err=err), mock.patch("urllib.request.urlopen", side_effect=err), \
                    self.assertRaises(audit_trust.TrustError):
                audit_trust.fetch("https://c.example", "tok")

    def test_trusted_keeps_counters_of_the_same_key(self) -> None:
        pk = Passkey()
        current = audit_trust.Trust(origin=ORIGIN, rp_id=RP, passkeys=[
            audit_trust.TrustedPasskey(pk.id, b64url_encode(pk.cose), 9)])
        fetched = [audit_trust.TrustedPasskey(pk.id, b64url_encode(pk.cose))]
        got = audit_trust.trusted("https://console.example", fetched, current)
        self.assertEqual(9, got.passkeys[0].sign_count)
        self.assertEqual((ORIGIN, RP), (got.origin, got.rp_id))


class TestCli(_Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.pk = Passkey()
        patcher = mock.patch.object(audit_trust, "audit_passkeys_path",
                                    return_value=self.trust_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetched(self) -> list[audit_trust.TrustedPasskey]:
        return [audit_trust.TrustedPasskey(self.pk.id, b64url_encode(self.pk.cose))]

    def test_trust_only_when_confirmed(self) -> None:
        with mock.patch.object(audit_trust, "fetch", return_value=self._fetched()), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(1, audit_passkeys.trust_from_console(
                "https://console.example", "t", confirm=lambda _p: False))
            self.assertFalse(self.trust_path.exists())
            self.assertEqual(0, audit_passkeys.trust_from_console(
                "https://console.example", "t", confirm=lambda _p: True))
        self.assertIn(fingerprint(self.pk.cose), err.getvalue())
        self.assertEqual([self.pk.id], [p.id for p in audit_trust.load().passkeys])

    def test_trust_errors(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()):
            with mock.patch.object(audit_trust, "fetch", side_effect=audit_trust.TrustError("x")):
                self.assertEqual(1, audit_passkeys.trust_from_console("https://c.example", "t"))
            with mock.patch.object(audit_trust, "fetch", return_value=[]):
                self.assertEqual(1, audit_passkeys.trust_from_console("https://c.example", "t"))
            with mock.patch.object(audit_trust, "fetch", return_value=self._fetched()):
                self.assertEqual(1, audit_passkeys.trust_from_console(
                    "http://c.example", "t", confirm=lambda _p: True))

    def test_run_lists_and_needs_a_terminal_to_trust(self) -> None:
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(0, audit_passkeys.run(False))
        self.assertIn("No passkeys trusted", out.getvalue())
        self.trust(self.pk)
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(0, audit_passkeys.run(False))
        self.assertIn(fingerprint(self.pk.cose), out.getvalue())
        with mock.patch("sys.stderr", io.StringIO()), \
                mock.patch.object(audit_passkeys.pairing, "load_console",
                                  return_value={"url": "https://c.example", "access_token": "t"}), \
                mock.patch("sys.stdin", io.StringIO()):
            self.assertEqual(1, audit_passkeys.run(True))
        with mock.patch("sys.stderr", io.StringIO()), \
                mock.patch.object(audit_passkeys.pairing, "load_console",
                                  side_effect=audit_passkeys.pairing.RemoteError("not paired")):
            self.assertEqual(1, audit_passkeys.run(True))

    def test_after_pairing_without_a_terminal(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()) as err, mock.patch("sys.stdin", io.StringIO()):
            audit_passkeys.after_pairing("https://c.example", "t")
        self.assertIn("audit passkeys --trust", err.getvalue())
        self.assertIn("Back it up", err.getvalue())


class TestAuditKey(_Tmp):
    def test_created_once_private_and_checked(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.dir)}):
            key = paths.audit_key_bytes()
            self.assertEqual(32, len(key))
            self.assertEqual(key, paths.audit_key_bytes())
            self.assertEqual(0o600, stat.S_IMODE(paths.audit_key_path().stat().st_mode))
            self.assertEqual(key, load_key())
            paths.audit_key_path().write_text("zz")
            with self.assertRaises(ValueError):
                paths.audit_key_bytes()
            self.assertIsNone(load_key())
            paths.audit_key_path().write_text("ab")
            with self.assertRaises(ValueError):
                paths.audit_key_bytes()


if __name__ == "__main__":
    unittest.main()
