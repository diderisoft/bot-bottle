"""Unit: CLI commands, run as the CLI runs them (`as_cli`), reach host state
only through the host controller (ADR 0020). The host controller's routes are
served in this process (`serve_host_controller`) on their own threads, so
any host DB connection opened on the test's thread is the CLI opening it."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bot_bottle import bottle_state
from bot_bottle.cli.commands import cleanup as cleanup_cmd
from bot_bottle.cli.commands import forge as forge_cmd
from bot_bottle.cli.commands import start as start_cmd
from bot_bottle.control.hostctl import runner
from bot_bottle.forge_connections import ForgeConnection, ForgeStore, ForgeUser
from bot_bottle.git_gate.state import GitGateStore
from bot_bottle.lib.db_store import DbStore
from bot_bottle.manifest import ManifestIndex
from bot_bottle.paths import host_db_path
from tests.unit import as_cli, bottled_agent, serve_host_controller, use_bottle_root


def _manifest() -> ManifestIndex:
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {"demo": {"skills": [], "prompt": "hello"}},
    })


class _AsTheCli(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.addCleanup(use_bottle_root(self.root / ".bot-bottle"))
        self.enterContext(mock.patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}))
        serve_host_controller(self)

    def as_cli(self) -> None:
        """From here on this test is the CLI: shared code asks the host
        controller, and the test thread opening the host DB fails it."""
        as_cli(self)
        real = DbStore._connect  # pylint: disable=protected-access
        test_thread = threading.current_thread()

        def connect(store: DbStore):  # type: ignore[no-untyped-def]
            if threading.current_thread() is test_thread and store.db_path == host_db_path():
                self.fail(f"the CLI opened the host DB ({type(store).__name__})")
            return real(store)

        self.enterContext(mock.patch.object(DbStore, "_connect", connect))
        # The served host controller is the running one.
        self.enterContext(mock.patch.object(runner, "running",
                                            return_value=runner.ensure_running()))


class TestStartDryRun(_AsTheCli):
    def test_it_prepares_through_the_host_controller_and_leaves_nothing(self) -> None:
        self.as_cli()
        written: list[str] = []
        real_write = bottle_state.write_metadata

        def write(metadata: bottle_state.BottleMetadata) -> None:
            real_write(metadata)
            written.append(metadata.identity)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(start_cmd.ManifestIndex, "resolve", return_value=_manifest()), \
                mock.patch("bot_bottle.backend.docker.resolve_plan.docker_mod.require_docker"), \
                mock.patch("bot_bottle.backend.docker.resolve_plan.docker_mod.runsc_available",
                           return_value=False), \
                mock.patch.object(bottle_state, "write_metadata", write), \
                mock.patch("bot_bottle.backend.resolve_common.write_metadata", write), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                redirect_stdout(out), redirect_stderr(err):
            code = start_cmd.cmd_start(["demo", "--bottle", "dev", "--dry-run"])
        self.assertEqual(0, code, err.getvalue())
        self.assertIn("dry-run requested", err.getvalue())
        self.assertEqual(1, len(written))
        # Recorded through the host controller, then removed through it.
        self.assertEqual([], bottle_state.all_metadata())
        # Its git-gate trailer is the launch's to keep: a dry run keeps none.
        trailer: list[tuple[str, str]] = []
        reader = threading.Thread(target=lambda: trailer.append(
            GitGateStore(host_db_path()).trailer(written[0])))
        reader.start()
        reader.join()
        self.assertEqual([("", "")], trailer)
        self.assertFalse(bottle_state.bottle_state_dir(written[0]).exists())


class TestCleanup(_AsTheCli):
    def test_it_removes_orphans_through_the_host_controller(self) -> None:
        bottled_agent("gone-1", backend="docker")
        bottle_state.write_per_bottle_dockerfile("gone-1", "FROM x\n")
        bottle_state.write_per_bottle_dockerfile("stray-1", "FROM x\n")  # no row
        self.as_cli()
        backend = mock.MagicMock()
        backend.enumerate_active.return_value = []
        backend.prepare_cleanup.return_value = mock.MagicMock(empty=True)
        with mock.patch.object(cleanup_cmd, "known_backend_names", return_value=("docker",)), \
                mock.patch.object(cleanup_cmd, "has_backend", return_value=True), \
                mock.patch.object(cleanup_cmd, "get_bottle_backend", return_value=backend), \
                mock.patch.object(cleanup_cmd, "_prompt_yes", return_value=True), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(0, cleanup_cmd.cmd_cleanup(["bots"]), err.getvalue())
        self.assertEqual([], bottle_state.all_metadata())
        self.assertFalse(bottle_state.bottle_state_dir("gone-1").exists())
        self.assertFalse(bottle_state.bottle_state_dir("stray-1").exists())


class TestForgeList(_AsTheCli):
    def test_it_lists_what_the_host_controller_has(self) -> None:
        ForgeStore(host_db_path()).add_user(
            ForgeConnection(host="gitea.example.com", kind="gitea",
                            url="https://gitea.example.com", ssh="ssh://git@10.0.4.2:30009"),
            ForgeUser("alice", "GITEA_EXAMPLE_COM_TOKEN"))
        self.as_cli()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(0, forge_cmd.cmd_forge(["list"]))
        self.assertIn("gitea.example.com", out.getvalue())
        self.assertIn("alice", out.getvalue())


if __name__ == "__main__":
    unittest.main()
