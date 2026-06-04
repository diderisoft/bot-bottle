"""Unit: ephemeral local-registry helper (PRD 0023 chunk 4c).

The helper brings up a `registry:2.8.3` container on a private
docker network with a random host-side port, yields a
`RegistryHandle`, and tears the container + network down on exit.
Tests mock `subprocess.run` + `socket.create_connection` so they
run without docker."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend.smolmachines import local_registry


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:  # type: ignore
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:  # type: ignore
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
    )


# Run sequence per ephemeral_registry() call:
#   docker network create  -> ok
#   docker run -d (registry)  -> ok (container id)
#   docker port (host port)  -> ok (mapping line)
#   docker rm -f (registry)  -> ok (in finally)
#   docker network rm  -> ok (in finally)
def _stock_run_sequence(port_line: str = "0.0.0.0:54321\n"):
    return [
        _ok(),                            # docker network create
        _ok(stdout="<container-id>\n"),   # docker run
        _ok(stdout=port_line),            # docker port
        _ok(),                            # docker rm -f
        _ok(),                            # docker network rm
    ]


class TestEphemeralRegistry(unittest.TestCase):
    def test_yields_handle_with_network_and_endpoints(self):
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(),
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry() as handle:
                # push_endpoint points at the registry container by
                # its docker-network name on its container port.
                self.assertTrue(
                    handle.push_endpoint.startswith(
                        "bot-bottle-registry-"
                    )
                )
                self.assertTrue(handle.push_endpoint.endswith(":5000"))
                # pull_endpoint is the host-side mapping for smolvm.
                self.assertEqual("localhost:54321", handle.pull_endpoint)
                # network name is the per-session bridge crane joins.
                self.assertTrue(
                    handle.network.startswith("bot-bottle-registry-net-")
                )
        # docker network create + docker run + docker port + rm -f + network rm
        self.assertEqual(5, run.call_count)

    def test_registry_run_publishes_random_port_across_interfaces(self):
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(),
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry():
                pass
        # second call is the docker run for the registry
        run_argv = run.call_args_list[1].args[0]
        self.assertEqual(["docker", "run"], run_argv[:2])
        self.assertIn("--rm", run_argv)
        # `-p 5000` (no IP prefix) — needed so the host-published
        # port is reachable from BOTH the host (for smolvm) and the
        # docker daemon (for the docker port command to find it).
        self.assertIn("5000", run_argv)
        # And the registry is attached to the same per-session
        # network the crane push container joins.
        self.assertIn("--network", run_argv)

    def test_force_removes_container_and_network_on_clean_exit(self):
        with patch.object(
            local_registry.subprocess, "run",
            side_effect=_stock_run_sequence(),
        ) as run, patch.object(
            local_registry.socket, "create_connection",
            return_value=_FakeSocket(),
        ):
            with local_registry.ephemeral_registry():
                pass

        # Last two calls are `docker rm -f <container>` then
        # `docker network rm <network>`.
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertEqual(["docker", "rm", "-f"], argvs[-2][:3])
        self.assertEqual(["docker", "network", "rm"], argvs[-1][:3])

    def test_force_removes_on_exception_inside_with(self):
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

        # Both teardowns still ran.
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertEqual(["docker", "rm", "-f"], argvs[-2][:3])
        self.assertEqual(["docker", "network", "rm"], argvs[-1][:3])

    def test_wait_ready_times_out(self):
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
        # Teardown still ran via the finally blocks.
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertEqual(["docker", "rm", "-f"], argvs[-2][:3])
        self.assertEqual(["docker", "network", "rm"], argvs[-1][:3])

    def test_unique_session_ids_per_call(self):
        sessions: list[tuple[str, str]] = []

        def capture(argv, *a, **kw):  # type: ignore
            if argv[:3] == ["docker", "network", "create"]:
                return _ok()
            if argv[:2] == ["docker", "run"]:
                # `--name <registry-name>` and `--network <net-name>`
                # both encode the session id.
                name = argv[argv.index("--name") + 1]
                network = argv[argv.index("--network") + 1]
                sessions.append((name, network))
                return _ok(stdout="cid\n")
            if argv[:2] == ["docker", "port"]:
                return _ok(stdout="0.0.0.0:1\n")
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

        self.assertEqual(2, len(sessions))
        self.assertNotEqual(sessions[0], sessions[1])


class TestCranePushTarball(unittest.TestCase):
    def test_runs_crane_container_on_registry_network_with_insecure_flag(self):
        handle = local_registry.RegistryHandle(
            network="cb-registry-net-x",
            push_endpoint="cb-registry-x:5000",
            pull_endpoint="localhost:54321",
        )
        with patch.object(
            local_registry.subprocess, "run", return_value=_ok(),
        ) as run:
            local_registry.crane_push_tarball(
                handle, "/tmp/img.tar", "cb-registry-x:5000/cb:abc",
            )

        argv = run.call_args.args[0]
        # Joined to the same docker network so it can reach the
        # registry by container name (no host port-forward needed
        # for the push leg).
        self.assertEqual("docker", argv[0])
        self.assertEqual("run", argv[1])
        self.assertIn("--rm", argv)
        self.assertIn("--network", argv)
        self.assertEqual(
            "cb-registry-net-x", argv[argv.index("--network") + 1],
        )
        # The tarball is mounted read-only at /img.tar.
        self.assertIn("-v", argv)
        self.assertIn("/tmp/img.tar:/img.tar:ro", argv)
        # And the crane command itself uses --insecure so plain
        # HTTP is allowed against the registry container.
        self.assertIn("push", argv)
        self.assertIn("--insecure", argv)
        self.assertIn("/img.tar", argv)
        self.assertIn("cb-registry-x:5000/cb:abc", argv)

    def test_dies_when_crane_returns_non_zero(self):
        handle = local_registry.RegistryHandle(
            network="cb-net", push_endpoint="cb:5000", pull_endpoint="localhost:1",
        )
        with patch.object(
            local_registry.subprocess, "run",
            return_value=_fail("push failed"),
        ), patch.object(
            local_registry, "die", side_effect=SystemExit("die"),
        ) as die:
            with self.assertRaises(SystemExit):
                local_registry.crane_push_tarball(
                    handle, "/tmp/img.tar", "cb:5000/cb:abc",
                )
            die.assert_called_once()
            # Error message names what was being pushed where.
            msg = die.call_args.args[0]
            self.assertIn("/tmp/img.tar", msg)
            self.assertIn("cb:5000/cb:abc", msg)


class _FakeSocket:
    """Minimal context-manager stand-in for the socket
    `create_connection` returns. The helper only uses `with` on it
    and discards the value, so we don't need any real network."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # type: ignore
        return False


if __name__ == "__main__":
    unittest.main()
