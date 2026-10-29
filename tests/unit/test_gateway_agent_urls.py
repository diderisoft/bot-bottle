"""Unit: `gateway_agent_urls` gives every bottle its git-gate address, so a
repo granted while it runs (PRD 0105) has a gate to rewrite to."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, cast

from bot_bottle.backend.launch_common import gateway_agent_urls


class GatewayAgentUrlsTests(unittest.TestCase):
    def test_a_bottle_without_repos_still_gets_the_gate(self) -> None:
        plan = cast(Any, SimpleNamespace(supervise_plan=None))
        self.assertEqual(("http://bot-bottle-gateway:9420", ""),
                         gateway_agent_urls(plan, "bot-bottle-gateway"))

    def test_supervise_is_set_only_when_the_bottle_uses_it(self) -> None:
        plan = cast(Any, SimpleNamespace(supervise_plan=object()))
        self.assertEqual("http://10.0.0.1:9100/", gateway_agent_urls(plan, "10.0.0.1")[1])


if __name__ == "__main__":
    unittest.main()
