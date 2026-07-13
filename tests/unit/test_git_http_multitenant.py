"""Unit: resolve_repo_root — source-IP-keyed git-gate repo namespace (PRD 0070).

The consolidated git-http backend serves every bottle from one process and
selects each request's repo root by the calling bottle's source IP. This is
the fail-closed selection logic, tested without a live server.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from bot_bottle.git_http_backend import resolve_repo_root
from bot_bottle.policy_resolver import PolicyResolveError

_BASE = Path("/git")


class _FakeResolver:
    def __init__(self, bottle_id: str | None = None, raises: bool = False) -> None:
        self._bottle_id = bottle_id
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def resolve_bottle_id(self, source_ip: str, identity_token: str = "") -> str | None:
        self.calls.append((source_ip, identity_token))
        if self._raises:
            raise PolicyResolveError("orchestrator down")
        return self._bottle_id


class TestResolveRepoRoot(unittest.TestCase):
    def test_single_tenant_passthrough(self) -> None:
        # No resolver → the flat base root, unchanged (legacy per-bottle mode).
        self.assertEqual(_BASE, resolve_repo_root(None, _BASE, "10.243.0.1"))

    def test_attributed_bottle_gets_namespaced_root(self) -> None:
        root = resolve_repo_root(_FakeResolver(bottle_id="ab12cd34"), _BASE, "10.243.0.1")
        self.assertEqual(Path("/git/ab12cd34"), root)

    def test_unattributed_denies(self) -> None:
        self.assertIsNone(resolve_repo_root(_FakeResolver(bottle_id=None), _BASE, "10.243.0.9"))

    def test_empty_bottle_id_denies(self) -> None:
        self.assertIsNone(resolve_repo_root(_FakeResolver(bottle_id=""), _BASE, "10.243.0.1"))

    def test_resolver_error_denies(self) -> None:
        # Orchestrator unreachable/errored must never serve repos.
        self.assertIsNone(resolve_repo_root(_FakeResolver(raises=True), _BASE, "10.243.0.1"))

    def test_namespace_escape_denies(self) -> None:
        # A bottle_id that would climb out of the root is rejected (defense in
        # depth — registry ids are token_hex, but never trust the namespace).
        self.assertIsNone(
            resolve_repo_root(_FakeResolver(bottle_id="../etc"), _BASE, "10.243.0.1")
        )

    def test_forwards_source_ip_and_token(self) -> None:
        r = _FakeResolver(bottle_id="b1")
        resolve_repo_root(r, _BASE, "10.243.0.1", "tok")
        self.assertEqual(("10.243.0.1", "tok"), r.calls[0])


if __name__ == "__main__":
    unittest.main()
