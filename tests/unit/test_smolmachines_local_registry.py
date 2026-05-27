"""Unit: ephemeral local-registry helper (PRD 0023 chunk 4c).

The helper brings up a `registry:2.8.3` container on a random
host port, yields a `(daemon_endpoint, host_endpoint)` pair, and
tears the container down on exit. Tests mock `subprocess.run` +
`socket.create_connection` so they run without docker."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from claude_bottle.backend.smolmachines import local_registry


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


# `docker info` always runs once per ephemeral_registry() to pick
# the daemon-side hostname; the run sequence is therefore
# (docker run, docker port, docker info, docker rm). Helpers below
# build a stock side_effect that covers all four.
def _stock_run_sequence(
    *,
    port: str = "0.0.0.0:54321\n",
    operating_system: str = "Docker Desktop\n",
):
    return [
        _ok(stdout="<container-id>\n"),  # docker run
        _ok(stdout=port),                # docker port
        _ok(stdout=operating_system),    # docker info
        _ok(),                           # docker rm -f
    ]


class TestEphemeralRegistry(unittest.TestCase):
    def test_yields_endpoints_with_docker_desktop_routing(self):
        # On Docker Desktop the daemon runs in its own VM, so the
        # registry has to be addressed by host.docker.internal for
        # docker push to work; smolvm (host process) still uses
        # localhost.
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(operating_system="Docker Desktop\n"),
        ), patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry() as endpoints:
                self.assertEqual(
                    "host.docker.internal:54321", endpoints.daemon_endpoint,
                )
                self.assertEqual(
                    "localhost:54321", endpoints.host_endpoint,
                )

    def test_yields_endpoints_with_native_linux_routing(self):
        # On a native Linux daemon the daemon shares the host's
        # network namespace, so localhost reaches the registry from
        # both sides.
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(
                operating_system="Debian GNU/Linux 12 (bookworm)\n",
            ),
        ), patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry() as endpoints:
                self.assertEqual(
                    "localhost:54321", endpoints.daemon_endpoint,
                )
                self.assertEqual(
                    "localhost:54321", endpoints.host_endpoint,
                )

    def test_runs_docker_with_all_interface_bind(self):
        # `-p 5000` (no IP prefix) binds the container's port 5000
        # on a random host port across all interfaces — needed so
        # Docker Desktop's daemon can reach the registry via
        # host.docker.internal. The 127.0.0.1-only bind we used
        # previously was invisible to the daemon's VM.
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(),
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry():
                pass

        run_argv = run.call_args_list[0].args[0]
        self.assertEqual(["docker", "run"], run_argv[:2])
        self.assertIn("--rm", run_argv)
        self.assertIn("5000", run_argv)
        # Explicitly NOT the loopback-only form — that one's broken
        # under Docker Desktop.
        self.assertNotIn("127.0.0.1::5000", run_argv)

    def test_force_removes_container_on_clean_exit(self):
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(),
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
            side_effect=_stock_run_sequence(),
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
            side_effect=_stock_run_sequence(),
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
                return _ok(stdout="cid\n")
            if argv[:2] == ["docker", "port"]:
                return _ok(stdout="0.0.0.0:1\n")
            if argv[:2] == ["docker", "info"]:
                return _ok(stdout="Docker Desktop\n")
            return _ok()

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
