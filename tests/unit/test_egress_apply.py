"""Unit: validate_routes_content (issue #198: _merge_single_route and
add_route removed; docker exec / cp / kill paths are covered by the
integration test)."""

import unittest

from bot_bottle.backend.docker.egress_apply import (
    EgressApplyError,
    validate_routes_content,
)


_ROUTES_EMPTY = "routes: []\n"
_ROUTES_ONE = 'routes:\n  - host: "api.anthropic.com"\n'


class TestValidateRoutesContent(unittest.TestCase):
    def test_accepts_minimal_route_table(self):
        validate_routes_content(_ROUTES_EMPTY)
        validate_routes_content(_ROUTES_ONE)

    def test_accepts_full_route_with_matches(self):
        validate_routes_content(
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
            validate_routes_content("routes:\n\t- host: x\n")
        self.assertIn("not valid", str(cm.exception))

    def test_rejects_missing_routes_key(self):
        with self.assertRaises(EgressApplyError):
            validate_routes_content("other: []\n")

    def test_rejects_non_list_routes(self):
        with self.assertRaises(EgressApplyError):
            validate_routes_content('routes: "not a list"\n')

    def test_rejects_partial_auth_pair(self):
        with self.assertRaises(EgressApplyError):
            validate_routes_content(
                'routes:\n'
                '  - host: "x.example"\n'
                '    auth_scheme: "Bearer"\n'
            )


if __name__ == "__main__":
    unittest.main()
