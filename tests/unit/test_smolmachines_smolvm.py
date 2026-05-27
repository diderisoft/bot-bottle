"""Unit: smolvm subprocess wrapper (PRD 0023 chunk 2b).

The wrapper is one thin function per smolvm CLI subcommand. Tests
mock `subprocess.run` and assert on the constructed argv +
SmolvmError raising on non-zero. The actual smolvm binary's
behavior is exercised in chunk 2d's integration smoke test."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_bottle.backend.smolmachines.smolvm import (
    SmolvmError,
    SmolvmRunResult,
    is_available,
    machine_cp,
    machine_create,
    machine_delete,
    machine_exec,
    machine_start,
    machine_stop,
    pack_create,
)


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=stderr,
    )


class TestArgvShapes(unittest.TestCase):
    """The CLI mixes `--name NAME` and positional-NAME styles
    across subcommands. The wrapper hides that inconsistency
    behind a uniform `name=` kwarg; lock down which form lands
    in each argv."""

    def _patch_run(self):
        return patch(
            "claude_bottle.backend.smolmachines.smolvm.subprocess.run",
            return_value=_ok(),
        )

    def test_pack_create_argv(self):
        with self._patch_run() as m:
            pack_create("claude-bottle:latest", Path("/tmp/agent.smolmachine"))
        argv = m.call_args.args[0]
        self.assertEqual(
            ["smolvm", "pack", "create",
             "--image", "claude-bottle:latest",
             "-o", "/tmp/agent.smolmachine"],
            argv,
        )

    def test_machine_create_minimal(self):
        with self._patch_run() as m:
            machine_create("agent-xyz")
        self.assertEqual(
            ["smolvm", "machine", "create", "agent-xyz"],
            m.call_args.args[0],
        )

    def test_machine_create_with_from_and_allow_cidr_and_env(self):
        with self._patch_run() as m:
            machine_create(
                "agent-xyz",
                from_path=Path("/stage/agent.smolmachine"),
                allow_cidrs=["192.168.50.2/32"],
                env={"HTTPS_PROXY": "http://192.168.50.2:8888"},
            )
        argv = m.call_args.args[0]
        # --from + --allow-cidr + -e are all flags, name is positional.
        self.assertEqual("smolvm", argv[0])
        self.assertIn("--from", argv)
        self.assertIn("/stage/agent.smolmachine", argv)
        # `--net` is explicit because smolvm 0.8.0's implied-net
        # from --allow-cidr doesn't fire when --from is set.
        self.assertIn("--net", argv)
        self.assertIn("--allow-cidr", argv)
        self.assertIn("192.168.50.2/32", argv)
        self.assertIn("-e", argv)
        self.assertIn("HTTPS_PROXY=http://192.168.50.2:8888", argv)
        self.assertEqual("agent-xyz", argv[-1])

    def test_machine_create_omits_net_when_no_allow_cidrs(self):
        with self._patch_run() as m:
            machine_create("agent-xyz", from_path=Path("/x.smolmachine"))
        self.assertNotIn("--net", m.call_args.args[0])

    def test_machine_start_uses_dash_name(self):
        # `start` is the --name flag form, NOT positional.
        with self._patch_run() as m:
            machine_start("agent-xyz")
        self.assertEqual(
            ["smolvm", "machine", "start", "--name", "agent-xyz"],
            m.call_args.args[0],
        )

    def test_machine_stop_uses_dash_name(self):
        with self._patch_run() as m:
            machine_stop("agent-xyz")
        self.assertEqual(
            ["smolvm", "machine", "stop", "--name", "agent-xyz"],
            m.call_args.args[0],
        )

    def test_machine_delete_uses_positional_name_and_force(self):
        # delete NAME is positional; -f required so no interactive
        # confirmation blocks teardown.
        with self._patch_run() as m:
            machine_delete("agent-xyz")
        self.assertEqual(
            ["smolvm", "machine", "delete", "-f", "agent-xyz"],
            m.call_args.args[0],
        )

    def test_machine_exec_argv_with_separator(self):
        # `--` separator before the command — smolvm's flag parser
        # would otherwise grab argv items that look like flags.
        with self._patch_run() as m:
            machine_exec("agent-xyz", ["echo", "hello"])
        self.assertEqual(
            ["smolvm", "machine", "exec", "--name", "agent-xyz",
             "--", "echo", "hello"],
            m.call_args.args[0],
        )

    def test_machine_exec_env_workdir_timeout(self):
        with self._patch_run() as m:
            machine_exec(
                "agent-xyz",
                ["ls"],
                env={"FOO": "bar", "BAZ": "qux"},
                workdir="/app",
                timeout="30s",
            )
        argv = m.call_args.args[0]
        self.assertIn("-w", argv); self.assertIn("/app", argv)
        self.assertIn("--timeout", argv); self.assertIn("30s", argv)
        # Both env vars present as -e K=V pairs.
        for pair in ("FOO=bar", "BAZ=qux"):
            self.assertIn(pair, argv)

    def test_machine_cp_positional_argv(self):
        with self._patch_run() as m:
            machine_cp("/host/file", "agent-xyz:/dest/file")
        self.assertEqual(
            ["smolvm", "machine", "cp",
             "/host/file", "agent-xyz:/dest/file"],
            m.call_args.args[0],
        )

    def test_machine_cp_empty_is_noop(self):
        # Guard against an upstream caller passing an unset path —
        # cp with an empty string is meaningless and would just
        # confuse smolvm's error message.
        with self._patch_run() as m:
            machine_cp("", "agent-xyz:/dest")
            machine_cp("/host", "")
        self.assertEqual(0, m.call_count)


class TestErrorPath(unittest.TestCase):
    """`check=True` paths raise SmolvmError on non-zero; `exec` is
    the one path that returns a result regardless."""

    def test_create_failure_raises(self):
        with patch(
            "claude_bottle.backend.smolmachines.smolvm.subprocess.run",
            return_value=_fail("no such image"),
        ):
            with self.assertRaises(SmolvmError) as cm:
                machine_create("agent-xyz")
            self.assertEqual(1, cm.exception.returncode)
            self.assertIn("no such image", str(cm.exception))

    def test_pack_create_failure_raises(self):
        with patch(
            "claude_bottle.backend.smolmachines.smolvm.subprocess.run",
            return_value=_fail("pack failed"),
        ):
            with self.assertRaises(SmolvmError):
                pack_create("missing:tag", Path("/tmp/out"))

    def test_exec_failure_returns_result(self):
        # The in-VM command's exit code is what Bottle.exec sees;
        # `false` exiting non-zero is not a smolvm failure.
        with patch(
            "claude_bottle.backend.smolmachines.smolvm.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=42, stdout="", stderr="nope",
            ),
        ):
            r = machine_exec("agent-xyz", ["sh", "-c", "exit 42"])
        self.assertEqual(SmolvmRunResult(42, "", "nope"), r)


class TestIsAvailable(unittest.TestCase):
    def test_true_when_on_path(self):
        with patch(
            "claude_bottle.backend.smolmachines.smolvm.shutil.which",
            return_value="/usr/local/bin/smolvm",
        ):
            self.assertTrue(is_available())

    def test_false_when_missing(self):
        with patch(
            "claude_bottle.backend.smolmachines.smolvm.shutil.which",
            return_value=None,
        ):
            self.assertFalse(is_available())


if __name__ == "__main__":
    unittest.main()
