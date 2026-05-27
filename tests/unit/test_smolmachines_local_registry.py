"""Unit: ephemeral local-registry helper (PRD 0023 chunk 4c).

The helper brings up a `registry:2.8.3` container on a random
loopback port, yields the port, and tears the container down on
exit. Tests mock `subprocess.run` + `socket.create_connection` so
they run without docker."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import call, patch

from claude_bottle.backend.smolmachines import local_registry


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


class TestEphemeralRegistry(unittest.TestCase):
    def test_yields_host_port_parsed_from_docker_port(self):
        # docker run + docker port + docker rm in that order; the
        # port command returns `127.0.0.1:54321` for the loopback
        # binding.
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=[
                _ok(stdout="<container-id>\n"),
                _ok(stdout="127.0.0.1:54321\n"),
                _ok(),
            ],
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry() as port:
                self.assertEqual(54321, port)

        # docker run, docker port, docker rm -f
        self.assertEqual(3, run.call_count)
        run_argv = run.call_args_list[0].args[0]
        self.assertEqual(["docker", "run"], run_argv[:2])
        self.assertIn("--rm", run_argv)
        # Loopback-only port binding so the registry isn't exposed
        # on the LAN even briefly.
        self.assertIn("127.0.0.1::5000", run_argv)

    def test_force_removes_container_on_clean_exit(self):
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=[_ok(stdout="cid\n"), _ok(stdout="127.0.0.1:1234\n"), _ok()],
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry():
                pass

        # Last call is `docker rm -f <name>`.
        last_argv = run.call_args_list[-1].args[0]
        self.assertEqual(["docker", "rm", "-f"], last_argv[:3])

    def test_force_removes_container_on_exception_inside_with(self):
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=[_ok(stdout="cid\n"), _ok(stdout="127.0.0.1:1234\n"), _ok()],
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with self.assertRaises(RuntimeError):
                with local_registry.ephemeral_registry():
                    raise RuntimeError("inside with")

        # rm -f still ran on exception.
        last_argv = run.call_args_list[-1].args[0]
        self.assertEqual(["docker", "rm", "-f"], last_argv[:3])

    def test_wait_ready_times_out_when_socket_never_connects(self):
        # Drop the timeout to a value that fits the test budget.
        with patch.object(local_registry, "_READY_TIMEOUT_S", 0.1), patch.object(
            local_registry.subprocess, "run",
            side_effect=[_ok(stdout="cid\n"), _ok(stdout="127.0.0.1:1234\n"), _ok()],
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            side_effect=OSError("conn refused"),
        ), patch.object(
            local_registry, "die",
            side_effect=SystemExit("die called"),
        ) as die:
            with self.assertRaises(SystemExit):
                with local_registry.ephemeral_registry():
                    self.fail("yield reached despite unreachable registry")
            die.assert_called_once()
        # rm -f still ran (cleanup goes through the finally block).
        last_argv = run.call_args_list[-1].args[0]
        self.assertEqual(["docker", "rm", "-f"], last_argv[:3])

    def test_unique_container_name_per_call(self):
        names: list[str] = []

        def capture(argv, *a, **kw):
            if argv[:2] == ["docker", "run"]:
                names.append(argv[argv.index("--name") + 1])
            return _ok(stdout="cid\n" if argv[:2] == ["docker", "run"]
                       else "127.0.0.1:1\n")

        with patch.object(
            local_registry.subprocess, "run", side_effect=capture,
        ), patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry():
                pass
            with local_registry.ephemeral_registry():
                pass

        self.assertEqual(2, len(names))
        self.assertNotEqual(names[0], names[1])
        for n in names:
            self.assertTrue(n.startswith("claude-bottle-registry-"))


class _FakeSocket:
    """Minimal context-manager stand-in for the socket
    `create_connection` returns. The helper only uses `with` on it
    and discards the value, so we don't need any real network."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
