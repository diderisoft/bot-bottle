"""Unit: validate_routes_content (issue #198: _merge_single_route and
add_route removed; docker exec / cp / kill paths are covered by the
integration test)."""

import tempfile
import unittest
from pathlib import Path

from tests.unit import use_bottle_root
from bot_bottle.backend.egress_apply import EgressApplyError
from bot_bottle.backend.docker.egress_apply import applicator


_ROUTES_EMPTY = "routes: []\n"
_ROUTES_ONE = 'routes:\n  - host: "api.anthropic.com"\n'


class TestValidateRoutesContent(unittest.TestCase):
    def test_accepts_minimal_route_table(self):
        applicator.validate_routes_content(_ROUTES_EMPTY)
        applicator.validate_routes_content(_ROUTES_ONE)

    def test_accepts_full_route_with_matches(self):
        applicator.validate_routes_content(
            'routes:\n'
            '  - host: "api.github.com"\n'
            '    auth_scheme: "Bearer"\n'
            '    token_env: "EGRESS_TOKEN_0"\n'
            '    matches:\n'
            '      - paths:\n'
            '          - value: "/repos/x/"\n'
        )

    def test_rejects_bad_yaml(self):
        with self.assertRaises(EgressApplyError) as cm:
            applicator.validate_routes_content("routes:\n\t- host: x\n")
        self.assertIn("not valid", str(cm.exception))

    def test_rejects_missing_routes_key(self):
        with self.assertRaises(EgressApplyError):
            applicator.validate_routes_content("other: []\n")

    def test_rejects_non_list_routes(self):
        with self.assertRaises(EgressApplyError):
            applicator.validate_routes_content('routes: "not a list"\n')

    def test_rejects_partial_auth_pair(self):
        with self.assertRaises(EgressApplyError):
            applicator.validate_routes_content(
                'routes:\n'
                '  - host: "x.example"\n'
                '    auth_scheme: "Bearer"\n'
            )

    def test_rejects_log_full(self):
        with self.assertRaises(EgressApplyError) as cm:
            applicator.validate_routes_content(
                'log: 2\n'
                'routes:\n'
                '  - host: "x.example"\n'
            )
        self.assertIn("must not change egress logging", str(cm.exception))


class TestApplyRoutesChange(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="egress-apply-test.")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name) / ".bot-bottle"))

    def test_apply_routes_change_fails_closed_after_companion_removal(self):
        # The per-bottle companion container that live route-apply used to
        # signal was removed in the companion-container removal (#385); apply now
        # fails closed until the gateway-side apply lands.
        with self.assertRaises(EgressApplyError) as cm:
            applicator.apply_routes_change(
                "dev",
                "routes:\n  - host: google.com\n",
            )
        self.assertIn("consolidated gateway", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
