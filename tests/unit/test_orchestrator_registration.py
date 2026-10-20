"""Unit: consolidated registration inputs — egress policy round-trip (PRD 0070)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.gateway.egress.addon_core import LOG_BLOCKS, load_config
from bot_bottle.control.registration import (
    RegistrationInputs,
    egress_policy,
    registration_inputs,
)


def _plan(routes: tuple[EgressRoute, ...], *, slug: str = "demo", log: int = 0) -> EgressPlan:
    return EgressPlan(
        slug=slug,
        routes_path=Path("/unused/routes.yaml"),
        routes=routes,
        token_env_map={},
        log=log,
    )


class TestEgressPolicy(unittest.TestCase):
    def test_policy_round_trips_through_load_config(self) -> None:
        # The policy the gateway serves must parse back to the same allow-list
        # the per-bottle gateway applied — moving onto the shared gateway must
        # not change a bottle's egress.
        routes = (EgressRoute(host="api.example.com"), EgressRoute(host="pypi.org"))
        cfg = load_config(egress_policy(_plan(routes)))
        self.assertEqual(("api.example.com", "pypi.org"), tuple(r.host for r in cfg.routes))

    def test_policy_preserves_log_level(self) -> None:
        plan = _plan((EgressRoute(host="x.example.com"),), log=LOG_BLOCKS)
        self.assertEqual(LOG_BLOCKS, load_config(egress_policy(plan)).log)

    def test_empty_routes_yield_deny_all(self) -> None:
        cfg = load_config(egress_policy(_plan(())))
        self.assertEqual((), cfg.routes)  # no routes → default-deny


class TestRegistrationInputs(unittest.TestCase):
    def test_bundles_policy_and_slug_metadata(self) -> None:
        plan = _plan((EgressRoute(host="api.example.com"),), slug="my-bot")
        inputs = registration_inputs(plan)
        self.assertIsInstance(inputs, RegistrationInputs)
        self.assertEqual("my-bot", json.loads(inputs.metadata)["slug"])
        self.assertEqual(egress_policy(plan), inputs.policy)  # same blob egress_policy renders


if __name__ == "__main__":
    unittest.main()
