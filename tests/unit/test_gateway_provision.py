"""Unit: git-gate provisioning into the running shared gateway (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.backend.docker.gateway_provision import (
    GatewayProvisionError,
    deprovision_git_gate,
    provision_git_gate,
)
from bot_bottle.git_gate import GitGatePlan, GitGateUpstream

_RUN = "bot_bottle.backend.docker.gateway_provision.run_docker"


def _proc(returncode: int = 0, stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stderr=stderr)


def _recorder(calls: list[list[str]]):
    """A run_docker side_effect that records argv and returns success."""
    def fake(argv: list[str]) -> Mock:
        calls.append(argv)
        return _proc()
    return fake


def _plan(*upstreams: GitGateUpstream) -> GitGatePlan:
    return GitGatePlan(
        slug="demo",
        entrypoint_script=Path(),
        hook_script=Path(),
        access_hook_script=Path(),
        upstreams=tuple(upstreams),
    )


def _up(name: str, *, key: str = "/host/keys/id", known_hosts: str = "") -> GitGateUpstream:
    return GitGateUpstream(
        name=name,
        upstream_url=f"ssh://git@github.com/x/{name}.git",
        upstream_host="github.com",
        upstream_port="22",
        identity_file=key,
        known_host_key="",
        known_hosts_file=Path(known_hosts) if known_hosts else Path(),
    )


class TestProvisionGitGate(unittest.TestCase):
    def test_copies_creds_and_runs_namespaced_init(self) -> None:
        calls: list[list[str]] = []
        with patch(_RUN, side_effect=_recorder(calls)):
            provision_git_gate("gw", "bottle1", _plan(_up("foo", known_hosts="/host/kh")))

        cps = [c for c in calls if c[:2] == ["docker", "cp"]]
        self.assertIn(["docker", "cp", "/host/keys/id", "gw:/git-gate/creds/bottle1/foo-key"], cps)
        self.assertIn(
            ["docker", "cp", "/host/kh", "gw:/git-gate/creds/bottle1/foo-known_hosts"], cps,
        )
        # The init script runs in the gateway, namespaced under the bottle id.
        exec_scripts = [c for c in calls if c[:3] == ["docker", "exec", "gw"] and c[3] == "sh"]
        self.assertEqual(1, len(exec_scripts))
        self.assertIn("repo=/git/bottle1/${name}.git", exec_scripts[0][-1])

    def test_omits_known_hosts_copy_when_absent(self) -> None:
        calls: list[list[str]] = []
        with patch(_RUN, side_effect=_recorder(calls)):
            provision_git_gate("gw", "b1", _plan(_up("foo")))  # no known_hosts
        cps = [c for c in calls if c[:2] == ["docker", "cp"]]
        self.assertEqual(1, len(cps))  # only the key, not known_hosts
        self.assertTrue(cps[0][3].endswith("/foo-key"))

    def test_no_upstreams_is_noop(self) -> None:
        with patch(_RUN) as m:
            provision_git_gate("gw", "b1", _plan())
        m.assert_not_called()

    def test_raises_on_docker_failure(self) -> None:
        with patch(_RUN, return_value=_proc(returncode=1, stderr="boom")):
            with self.assertRaises(GatewayProvisionError):
                provision_git_gate("gw", "b1", _plan(_up("foo")))

    def test_rejects_unsafe_bottle_id_before_any_docker(self) -> None:
        with patch(_RUN) as m:
            with self.assertRaises(GatewayProvisionError):
                provision_git_gate("gw", "../etc", _plan(_up("foo")))
        m.assert_not_called()  # rejected before a single docker call


class TestDeprovision(unittest.TestCase):
    def test_removes_repo_and_creds(self) -> None:
        with patch(_RUN, return_value=_proc()) as m:
            deprovision_git_gate("gw", "b1")
        argv = m.call_args.args[0]
        self.assertEqual(["docker", "exec", "gw", "rm", "-rf"], argv[:5])
        self.assertIn("/git/b1", argv)
        self.assertIn("/git-gate/creds/b1", argv)

    def test_rejects_unsafe_bottle_id(self) -> None:
        with patch(_RUN) as m:
            with self.assertRaises(GatewayProvisionError):
                deprovision_git_gate("gw", "a/b")
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
