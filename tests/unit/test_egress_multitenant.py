"""Unit: fail-closed per-client egress resolution — config + context (PRD 0070)."""

from __future__ import annotations

import unittest

from bot_bottle.egress_addon_core import resolve_client_config, resolve_client_context
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
        self, policy: str | None = None, bottle_id: str | None = None, raises: bool = False,
    ) -> None:
        self._policy = policy
        self._bottle_id = bottle_id
        self._raises = raises

    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = "",
    ) -> tuple[str | None, str | None]:
        if self._raises:
            raise PolicyResolveError("orchestrator down")
        return self._policy, self._bottle_id


class TestResolveClientContext(unittest.TestCase):
    def test_returns_config_and_bottle_id(self) -> None:
        cfg, slug = resolve_client_context(
            _FakeContextResolver(policy="routes:\n  - host: example.com\n", bottle_id="b1"),
            "10.243.0.1",
        )
        self.assertEqual(("example.com",), tuple(r.host for r in cfg.routes))
        self.assertEqual("b1", slug)

    def test_unattributed_denies_and_empty_slug(self) -> None:
        cfg, slug = resolve_client_context(
            _FakeContextResolver(policy=None, bottle_id=None), "10.243.0.9",
        )
        self.assertEqual((), cfg.routes)
        self.assertEqual("", slug)  # no bottle → supervise unavailable

    def test_resolver_error_denies_and_empty_slug(self) -> None:
        cfg, slug = resolve_client_context(_FakeContextResolver(raises=True), "10.243.0.1")
        self.assertEqual((), cfg.routes)
        self.assertEqual("", slug)

    def test_unparseable_policy_denies_but_keeps_slug(self) -> None:
        # A bad policy denies egress, but the bottle is still attributed (its
        # supervise queue is keyed by the id, independent of route parsing).
        cfg, slug = resolve_client_context(
            _FakeContextResolver(policy="routes: notalist\n", bottle_id="b2"), "10.243.0.1",
        )
        self.assertEqual((), cfg.routes)
        self.assertEqual("b2", slug)


if __name__ == "__main__":
    unittest.main()
