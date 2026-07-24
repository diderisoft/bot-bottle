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

from bot_bottle import backend as backend_mod
from bot_bottle.backend import (
    ActiveAgent,
    enumerate_active_agents,
    get_bottle_backend,
    known_backend_names,
)


class TestGetBottleBackend(unittest.TestCase):
    def test_explicit_name_wins_over_env(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}):
            b = get_bottle_backend("docker")
        self.assertEqual("docker", b.name)

    def test_env_var_fallback(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}):
            b = get_bottle_backend()
        self.assertEqual("firecracker", b.name)

    def test_default_macos_container_when_available(self):
        class _FakeBackend:
            name = "macos-container"

            def is_available(self) -> bool:
                return True

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend(),
                 "docker": _FakeBackend(),
             }):
            b = get_bottle_backend()
        self.assertEqual("macos-container", b.name)

    def test_default_docker_when_no_macos_and_host_not_kvm(self):
        class _FakeBackend:
            def __init__(self, name: str, available: bool) -> None:
                self.name = name
                self._available = available

            def is_available(self) -> bool:
                return self._available

        # No macOS container and the host can't run firecracker (no
        # KVM / not Linux) → docker fallback with a prompt. Simulate
        # the user picking "d" (use docker anyway).
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod.FirecrackerBottleBackend,
                          "is_host_capable", classmethod(lambda cls: False)), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend("macos-container", False),
                 "docker": _FakeBackend("docker", True),
             }), \
             patch.object(backend_mod, "read_tty_line", return_value="d"):
            b = get_bottle_backend()
        self.assertEqual("docker", b.name)

    def test_default_firecracker_on_kvm_host_even_when_binary_missing(self):
        class _FakeBackend:
            def __init__(self, name: str, available: bool) -> None:
                self.name = name
                self._available = available

            def is_available(self) -> bool:
                return self._available

        # A KVM-capable Linux host defaults to firecracker even when the
        # binary isn't installed (is_available False) — start then prints
        # the install pointer instead of falling back to docker.
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod.FirecrackerBottleBackend,
                          "is_host_capable", classmethod(lambda cls: True)), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend("macos-container", False),
                 "firecracker": _FakeBackend("firecracker", False),
                 "docker": _FakeBackend("docker", True),
             }):
            b = get_bottle_backend()
        self.assertEqual("firecracker", b.name)

    def test_unknown_dies(self):
        with patch.object(backend_mod, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_bottle_backend("nonexistent")

    def test_no_backend_available_dies(self):
        # No VM and no docker → print install instructions and die.
        class _FakeBackend:
            def __init__(self, name: str, available: bool) -> None:
                self.name = name
                self._available = available

            def is_available(self) -> bool:
                return self._available

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod.FirecrackerBottleBackend,
                          "is_host_capable", classmethod(lambda cls: False)), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend("macos-container", False),
                 "docker": _FakeBackend("docker", False),
             }), \
             patch.object(backend_mod, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_bottle_backend()

    def test_docker_fallback_user_quits(self):
        # VM unavailable, docker available, user picks "q" → die.
        class _FakeBackend:
            def __init__(self, name: str, available: bool) -> None:
                self.name = name
                self._available = available

            def is_available(self) -> bool:
                return self._available

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod.FirecrackerBottleBackend,
                          "is_host_capable", classmethod(lambda cls: False)), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend("macos-container", False),
                 "docker": _FakeBackend("docker", True),
             }), \
             patch.object(backend_mod, "read_tty_line", return_value="q"), \
             patch.object(backend_mod, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_bottle_backend()


    def test_docker_fallback_non_interactive_dies(self):
        # prompt=False: headless/CI contexts must not block on a TTY read.
        class _FakeBackend:
            def __init__(self, name: str, available: bool) -> None:
                self.name = name
                self._available = available

            def is_available(self) -> bool:
                return self._available

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod.FirecrackerBottleBackend,
                          "is_host_capable", classmethod(lambda cls: False)), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend("macos-container", False),
                 "docker": _FakeBackend("docker", True),
             }), \
             patch.object(backend_mod, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_bottle_backend(prompt=False)

    def test_docker_fallback_user_picks_install(self):
        # User picks [i] → print install instructions then die.
        class _FakeBackend:
            def __init__(self, name: str, available: bool) -> None:
                self.name = name
                self._available = available

            def is_available(self) -> bool:
                return self._available

        with patch.dict(os.environ, {}, clear=True), \
             patch.object(backend_mod.FirecrackerBottleBackend,
                          "is_host_capable", classmethod(lambda cls: False)), \
             patch.object(backend_mod, "_backends", {
                 "macos-container": _FakeBackend("macos-container", False),
                 "docker": _FakeBackend("docker", True),
             }), \
             patch.object(backend_mod, "read_tty_line", return_value="i"), \
             patch.object(backend_mod, "_print_vm_install_instructions") as mock_inst, \
             patch.object(backend_mod, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_bottle_backend()
        mock_inst.assert_called_once()


class TestReadTtyLine(unittest.TestCase):
    """Unit tests for the shared read_tty_line helper in bot_bottle.util."""

    def test_reads_from_dev_tty(self):
        from unittest.mock import mock_open
        from bot_bottle.util import read_tty_line

        m = mock_open(read_data="hello\n")
        with patch("builtins.open", m):
            result = read_tty_line()
        self.assertEqual("hello", result)
        m.assert_called_once_with("/dev/tty", "r", encoding="utf-8")

    def test_falls_back_to_stdin_on_oserror(self):
        import io
        from bot_bottle.util import read_tty_line
        import sys as _sys

        with patch("builtins.open", side_effect=OSError("no tty")), \
             patch.object(_sys, "stdin", io.StringIO("world\n")):
            result = read_tty_line()
        self.assertEqual("world", result)


class TestPrintVmInstallInstructions(unittest.TestCase):
    """Unit tests for _print_vm_install_instructions platform branches."""

    def test_linux_prints_firecracker_instructions(self):
        from bot_bottle.backend import _print_vm_install_instructions

        with patch.object(backend_mod, "_platform_vm_suggestion",
                          return_value="firecracker"), \
             patch.object(backend_mod, "info") as mock_info:
            _print_vm_install_instructions()

        messages = [str(c[0][0]) for c in mock_info.call_args_list]
        self.assertTrue(any("Firecracker" in m for m in messages))

    def test_macos_prints_apple_container_instructions(self):
        from bot_bottle.backend import _print_vm_install_instructions

        with patch.object(backend_mod, "_platform_vm_suggestion",
                          return_value="macos-container"), \
             patch.object(backend_mod, "info") as mock_info:
            _print_vm_install_instructions()

        messages = [str(c[0][0]) for c in mock_info.call_args_list]
        self.assertTrue(any("Apple Container" in m for m in messages))


class TestKnownBackendNames(unittest.TestCase):
    def test_returns_backends_sorted(self):
        self.assertEqual(
            ("docker", "firecracker", "macos-container"),
            known_backend_names(),
        )


class TestEnumerateActiveAgents(unittest.TestCase):
    """Combines each backend's `enumerate_active`. Each backend's
    implementation has its own tests (`test_docker_enumerate_active`,
    `test_firecracker_backend`); this just asserts the aggregator
    stitches them together."""

    def test_concatenates_per_backend(self):
        a = ActiveAgent(
            backend_name="docker", slug="a-1", agent_name="impl",
            started_at="", services=("egress",),
        )
        b = ActiveAgent(
            backend_name="firecracker", slug="b-2", agent_name="research",
            started_at="", services=(),
        )

        class _FakeBackend:
            def __init__(self, items: object, available: object = True) -> None:  # type: ignore
                self._items = items
                self._available = available

            def is_available(self):
                return self._available

            def enumerate_active(self):
                return self._items

        with patch.object(
            backend_mod, "_backends",
            {"docker": _FakeBackend([a]), "firecracker": _FakeBackend([b])},
        ):
            self.assertEqual([a, b], enumerate_active_agents())

    def test_sorts_by_started_at_then_slug_across_backends(self):
        newer = ActiveAgent(
            backend_name="docker", slug="docker-new", agent_name="impl",
            started_at="2026-06-02T12:00:00Z", services=(),
        )
        tie_b = ActiveAgent(
            backend_name="docker", slug="b-slug", agent_name="review",
            started_at="2026-06-02T11:00:00Z", services=(),
        )
        missing_metadata = ActiveAgent(
            backend_name="firecracker", slug="missing-metadata",
            agent_name="?", started_at="", services=(),
        )
        tie_a = ActiveAgent(
            backend_name="firecracker", slug="a-slug", agent_name="research",
            started_at="2026-06-02T11:00:00Z", services=(),
        )

        class _FakeBackend:
            def __init__(self, items: object) -> None:  # type: ignore
                self._items = items

            def is_available(self) -> bool:
                return True

            def enumerate_active(self) -> object:
                return self._items

        with patch.object(
            backend_mod, "_backends",
            {
                "docker": _FakeBackend([newer, tie_b]),
                "firecracker": _FakeBackend([missing_metadata, tie_a]),
            },
        ):
            self.assertEqual(
                [missing_metadata, tie_a, tie_b, newer],
                enumerate_active_agents(),
            )

    def test_empty_when_no_backends_have_active(self):
        class _FakeBackend:
            def is_available(self):
                return True

            def enumerate_active(self):
                return []

        with patch.object(
            backend_mod, "_backends",
            {"docker": _FakeBackend(), "firecracker": _FakeBackend()},
        ):
            self.assertEqual([], enumerate_active_agents())

    def test_skips_unavailable_backends(self):
        # If a backend's runtime isn't installed (docker missing on a
        # firecracker host, or KVM missing on a docker-only host), the
        # cross-backend enumerator skips it rather than dying —
        # `has_backend` gates the iteration.
        present = ActiveAgent(
            backend_name="docker", slug="a-1", agent_name="impl",
            started_at="", services=(),
        )
        hidden = ActiveAgent(
            backend_name="firecracker", slug="x", agent_name="x",
            started_at="", services=(),
        )

        class _FakeBackend:
            def __init__(self, items: object, available: object) -> None:  # type: ignore
                self._items = items
                self._available = available

            def is_available(self) -> object:
                return self._available

            def enumerate_active(self):
                return self._items

        with patch.object(
            backend_mod, "_backends",
            {
                "docker": _FakeBackend([present], available=True),
                "firecracker": _FakeBackend([hidden], available=False),
            },
        ):
            self.assertEqual([present], enumerate_active_agents())


class TestHasBackend(unittest.TestCase):
    def test_known_backend_consults_is_available(self):
        class _FakeBackend:
            def is_available(self):
                return False

        with patch.object(
            backend_mod, "_backends", {"docker": _FakeBackend()},
        ):
            from bot_bottle.backend import has_backend
            self.assertFalse(has_backend("docker"))

    def test_unknown_backend_returns_false(self):
        from bot_bottle.backend import has_backend
        self.assertFalse(has_backend("nonexistent"))


class TestEnsureOrchestrator(unittest.TestCase):
    """The backend-agnostic orchestrator bring-up entry point. Docker starts
    the orchestrator + gateway containers; firecracker boots the infra VM;
    macos-container starts the infra container."""

    def test_docker_delegates_to_infra_service(self):
        b = get_bottle_backend("docker")
        with patch(
            "bot_bottle.backend.docker.infra.DockerInfraService"
        ) as service_cls:
            service_cls.return_value.ensure_running.return_value = (
                "http://127.0.0.1:8099"
            )
            url = b.ensure_orchestrator()
        self.assertEqual(url, "http://127.0.0.1:8099")
        service_cls.return_value.ensure_running.assert_called_once_with()

    def test_firecracker_delegates_to_infra_vm(self):
        b = get_bottle_backend("firecracker")
        with patch(
            "bot_bottle.backend.firecracker.infra_vm.ensure_running"
        ) as ensure_running:
            ensure_running.return_value.control_plane_url = (
                "http://10.243.255.1:8099"
            )
            url = b.ensure_orchestrator()
        self.assertEqual(url, "http://10.243.255.1:8099")

    def test_macos_delegates_to_infra_container(self):
        b = get_bottle_backend("macos-container")
        with patch(
            "bot_bottle.backend.macos_container.infra.MacosInfraService"
        ) as service_cls:
            service_cls.return_value.ensure_running.return_value.control_plane_url = (
                "http://192.168.128.2:8099"
            )
            url = b.ensure_orchestrator()
        self.assertEqual(url, "http://192.168.128.2:8099")


if __name__ == "__main__":
    unittest.main()
