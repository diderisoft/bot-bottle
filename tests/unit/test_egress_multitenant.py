"""Unit: fail-closed per-client egress resolution — config + context (PRD 0070)."""

from __future__ import annotations

import unittest

from bot_bottle.egress_addon_core import (
    DENY_RESOLVER_ERROR,
    DENY_UNATTRIBUTED,
    DENY_UNPARSEABLE,
    decide,
    resolve_client_config,
    resolve_client_context,
)
from bot_bottle.policy_resolver import PolicyResolveError


class _FakeResolver:
    def __init__(self, result: str | None = None, raises: bool = False) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def resolve(self, source_ip: str, identity_token: str = "") -> str | None:
        self.calls.append((source_ip, identity_token))
        if self._raises:
            raise PolicyResolveError("orchestrator down")
        return self._result


class TestResolveClientConfig(unittest.TestCase):
    def test_valid_policy_is_parsed(self) -> None:
        cfg = resolve_client_config(
            _FakeResolver(result="routes:\n  - host: example.com\n"), "10.243.0.1"
        )
        self.assertEqual(("example.com",), tuple(r.host for r in cfg.routes))

    def test_unattributed_none_denies_all(self) -> None:
        cfg = resolve_client_config(_FakeResolver(result=None), "10.243.0.9")
        self.assertEqual((), cfg.routes)  # no routes → default-deny

    def test_empty_policy_denies_all(self) -> None:
        self.assertEqual((), resolve_client_config(_FakeResolver(result=""), "10.243.0.1").routes)

    def test_resolver_error_denies_all(self) -> None:
        # Orchestrator unreachable/errored must never widen egress.
        self.assertEqual((), resolve_client_config(_FakeResolver(raises=True), "10.243.0.1").routes)

    def test_unparseable_policy_denies_all(self) -> None:
        cfg = resolve_client_config(_FakeResolver(result="routes: notalist\n"), "10.243.0.1")
        self.assertEqual((), cfg.routes)

    def test_forwards_source_ip_and_token(self) -> None:
        r = _FakeResolver(result=None)
        resolve_client_config(r, "10.243.0.1", "tok")
        self.assertEqual(("10.243.0.1", "tok"), r.calls[0])


class _FakeContextResolver:
    def __init__(
        self, policy: str | None = None, bottle_id: str | None = None,
        raises: bool = False, tokens: dict[str, str] | None = None,
    ) -> None:
        self._policy = policy
        self._bottle_id = bottle_id
        self._raises = raises
        self._tokens = tokens or {}

    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = "",
    ) -> tuple[str | None, str | None, dict[str, str]]:
        if self._raises:
            raise PolicyResolveError("orchestrator down")
        return self._policy, self._bottle_id, self._tokens


class TestResolveClientContext(unittest.TestCase):
    def test_returns_config_bottle_id_and_tokens(self) -> None:
        cfg, slug, tokens = resolve_client_context(
            _FakeContextResolver(
                policy="routes:\n  - host: example.com\n", bottle_id="b1",
                tokens={"EGRESS_TOKEN_0": "sekret"},
            ),
            "10.243.0.1",
        )
        self.assertEqual(("example.com",), tuple(r.host for r in cfg.routes))
        self.assertEqual("b1", slug)
        self.assertEqual({"EGRESS_TOKEN_0": "sekret"}, tokens)  # for auth injection

    def test_unattributed_denies_and_empty_slug(self) -> None:
        cfg, slug, tokens = resolve_client_context(
            _FakeContextResolver(policy=None, bottle_id=None), "10.243.0.9",
        )
        self.assertEqual((), cfg.routes)
        self.assertEqual("", slug)  # no bottle → supervise unavailable
        self.assertEqual({}, tokens)

    def test_resolver_error_denies_empty_slug_no_tokens(self) -> None:
        cfg, slug, tokens = resolve_client_context(
            _FakeContextResolver(raises=True), "10.243.0.1",
        )
        self.assertEqual((), cfg.routes)
        self.assertEqual("", slug)
        self.assertEqual({}, tokens)

    def test_unparseable_policy_denies_but_keeps_slug(self) -> None:
        # A bad policy denies egress, but the bottle is still attributed (its
        # supervise queue is keyed by the id, independent of route parsing).
        cfg, slug, _tokens = resolve_client_context(
            _FakeContextResolver(policy="routes: notalist\n", bottle_id="b2"), "10.243.0.1",
        )
        self.assertEqual((), cfg.routes)
        self.assertEqual("b2", slug)


if __name__ == "__main__":
    unittest.main()


class TestDenyReasonNamesTheRealFault(unittest.TestCase):
    """A deny-all must not masquerade as a missing allowlist entry.

    Regression: an unregistered bottle resolves no policy, so *every* host is
    denied — but the block message said `host X is not in the allowlist`,
    which reads as a config problem and sends the operator hunting for a route
    that was never missing. The structural reason wins over that wording.
    """

    def _reason(self, resolver: object, host: str = "chatgpt.com") -> str:
        cfg = resolve_client_config(resolver, "10.243.0.1")  # type: ignore[arg-type]
        return decide(cfg.routes, host, "/v1/x", {}, deny_reason=cfg.deny_reason).reason

    def test_unattributed_says_unattributed_not_allowlist(self) -> None:
        reason = self._reason(_FakeResolver(result=None))
        self.assertEqual(DENY_UNATTRIBUTED, reason)
        # The misleading claim is the one that must be gone: the host was
        # never "not in the allowlist" — there was no allowlist at all.
        self.assertNotIn("is not in the bottle's egress.routes allowlist", reason)
        # Both causes must be named. `/resolve` fail-closes on a missing row
        # *and* on a token mismatch, and the message pointing only at the row
        # sent us hunting for a deregistered bottle that was registered fine.
        self.assertIn("registry row", reason)
        self.assertIn("identity token", reason)

    def test_resolver_error_says_orchestrator_unreachable(self) -> None:
        self.assertEqual(DENY_RESOLVER_ERROR, self._reason(_FakeResolver(raises=True)))

    def test_unparseable_policy_says_so(self) -> None:
        self.assertEqual(
            DENY_UNPARSEABLE, self._reason(_FakeResolver(result="routes: notalist\n")))

    def test_a_real_allowlist_miss_keeps_the_allowlist_wording(self) -> None:
        """The message only changes for structural deny-alls — a loaded policy
        that genuinely lacks the host still points at the allowlist."""
        reason = self._reason(_FakeResolver(result='routes:\n  - host: "api.example.com"\n'))
        self.assertIn("is not in the bottle's egress.routes allowlist", reason)
        self.assertIn("chatgpt.com", reason)

    def test_allowed_host_is_still_forwarded(self) -> None:
        cfg = resolve_client_config(
            _FakeResolver(result='routes:\n  - host: "api.example.com"\n'), "10.243.0.1")
        decision = decide(
            cfg.routes, "api.example.com", "/v1/x", {}, deny_reason=cfg.deny_reason)
        self.assertEqual("forward", decision.action)

    def test_a_parsed_policy_carries_no_deny_reason(self) -> None:
        cfg = resolve_client_config(
            _FakeResolver(result='routes:\n  - host: "api.example.com"\n'), "10.243.0.1")
        self.assertEqual("", cfg.deny_reason)
