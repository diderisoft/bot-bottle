"""Unit: agent NO_PROXY value builder (PR #25 follow-up).

claude-code's HTTP MCP client must bypass pipelock for the supervise
sidecar — long-poll tool calls would hit pipelock's idle timeout
otherwise. This test pins the rule: localhost always; supervise iff
the supervise sidecar is in the plan."""

import unittest
from pathlib import Path

from claude_bottle.backend.docker.launch import _agent_no_proxy


class _FakePlan:
    """Just enough plan shape for the helper — no full DockerBottlePlan
    construction needed."""

    def __init__(self, supervise_plan):
        self.supervise_plan = supervise_plan


class _SentinelSupervisePlan:
    """The helper only checks `supervise_plan is not None`; any object
    is fine."""


class TestAgentNoProxy(unittest.TestCase):
    def test_loopback_only_when_no_supervise(self):
        self.assertEqual(
            "localhost,127.0.0.1",
            _agent_no_proxy(_FakePlan(supervise_plan=None)),
        )

    def test_supervise_appended_when_enabled(self):
        self.assertEqual(
            "localhost,127.0.0.1,supervise",
            _agent_no_proxy(_FakePlan(supervise_plan=_SentinelSupervisePlan())),
        )


if __name__ == "__main__":
    unittest.main()
