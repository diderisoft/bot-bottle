"""Unit: backend selection + cross-backend enumeration (issue #77).

`get_bottle_backend(name)` resolves a backend by explicit name,
env var, or default. `enumerate_active_agents()` walks every
registered backend and concatenates their `ActiveAgent`
listings — the CLI and dashboard both go through this so adding
a backend lights it up in both places."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from claude_bottle import backend as backend_mod
from claude_bottle.backend import (
    ActiveAgent,
    enumerate_active_agents,
    get_bottle_backend,
    known_backend_names,
)


class TestGetBottleBackend(unittest.TestCase):
    def test_explicit_name_wins_over_env(self):
        with patch.dict(os.environ, {"CLAUDE_BOTTLE_BACKEND": "smolmachines"}):
            b = get_bottle_backend("docker")
        self.assertEqual("docker", b.name)

    def test_env_var_fallback(self):
        with patch.dict(os.environ, {"CLAUDE_BOTTLE_BACKEND": "smolmachines"}):
            b = get_bottle_backend()
        self.assertEqual("smolmachines", b.name)

    def test_default_docker(self):
        with patch.dict(os.environ, {}, clear=True):
            b = get_bottle_backend()
        self.assertEqual("docker", b.name)

    def test_unknown_dies(self):
        with patch.object(backend_mod, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_bottle_backend("nonexistent")


class TestKnownBackendNames(unittest.TestCase):
    def test_returns_both_backends_sorted(self):
        self.assertEqual(("docker", "smolmachines"), known_backend_names())


class TestEnumerateActiveAgents(unittest.TestCase):
    """Combines each backend's `enumerate_active`. Each backend's
    implementation has its own tests (`test_docker_enumerate_active`,
    `test_smolmachines_*`); this just asserts the aggregator stitches
    them together."""

    def test_concatenates_per_backend(self):
        a = ActiveAgent(
            backend_name="docker", slug="a-1", agent_name="impl",
            started_at="", services=("pipelock",),
        )
        b = ActiveAgent(
            backend_name="smolmachines", slug="b-2", agent_name="research",
            started_at="", services=(),
        )

        class _FakeBackend:
            def __init__(self, items, available=True):
                self._items = items
                self._available = available

            def is_available(self):
                return self._available

            def enumerate_active(self):
                return self._items

        with patch.object(
            backend_mod, "_BACKENDS",
            {"docker": _FakeBackend([a]), "smolmachines": _FakeBackend([b])},
        ):
            self.assertEqual([a, b], enumerate_active_agents())

    def test_empty_when_no_backends_have_active(self):
        class _FakeBackend:
            def is_available(self):
                return True

            def enumerate_active(self):
                return []

        with patch.object(
            backend_mod, "_BACKENDS",
            {"docker": _FakeBackend(), "smolmachines": _FakeBackend()},
        ):
            self.assertEqual([], enumerate_active_agents())

    def test_skips_unavailable_backends(self):
        # If a backend's runtime isn't installed (smolvm missing on
        # a docker-only host, or docker missing on a smolmachines-
        # only host), the cross-backend enumerator skips it rather
        # than dying — `has_backend` gates the iteration.
        present = ActiveAgent(
            backend_name="docker", slug="a-1", agent_name="impl",
            started_at="", services=(),
        )
        hidden = ActiveAgent(
            backend_name="smolmachines", slug="x", agent_name="x",
            started_at="", services=(),
        )

        class _FakeBackend:
            def __init__(self, items, available):
                self._items = items
                self._available = available

            def is_available(self):
                return self._available

            def enumerate_active(self):
                return self._items

        with patch.object(
            backend_mod, "_BACKENDS",
            {
                "docker": _FakeBackend([present], available=True),
                "smolmachines": _FakeBackend([hidden], available=False),
            },
        ):
            self.assertEqual([present], enumerate_active_agents())


class TestHasBackend(unittest.TestCase):
    def test_known_backend_consults_is_available(self):
        class _FakeBackend:
            def is_available(self):
                return False

        with patch.object(
            backend_mod, "_BACKENDS", {"docker": _FakeBackend()},
        ):
            from claude_bottle.backend import has_backend
            self.assertFalse(has_backend("docker"))

    def test_unknown_backend_returns_false(self):
        from claude_bottle.backend import has_backend
        self.assertFalse(has_backend("nonexistent"))


if __name__ == "__main__":
    unittest.main()
