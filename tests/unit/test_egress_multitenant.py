"""Unit: resolve_client_config — fail-closed per-client egress config (PRD 0070)."""

from __future__ import annotations

import unittest

from bot_bottle.egress_addon_core import resolve_client_config
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


if __name__ == "__main__":
    unittest.main()
