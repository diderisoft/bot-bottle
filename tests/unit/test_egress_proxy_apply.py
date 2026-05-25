"""Unit: validate_routes_content (PRD 0014 retargeted by PRD 0017
chunk 3). docker exec / cp / kill paths are covered by the
integration test."""

import unittest

from claude_bottle.backend.docker.egress_proxy_apply import (
    EgressProxyApplyError,
    validate_routes_content,
)


class TestValidateRoutesContent(unittest.TestCase):
    def test_accepts_minimal_route_table(self):
        validate_routes_content('{"routes": []}')
        validate_routes_content(
            '{"routes": [{"host": "api.github.com"}]}'
        )

    def test_accepts_full_route(self):
        validate_routes_content(
            '{"routes": [{"host": "api.github.com",'
            ' "path_allowlist": ["/repos/x/"],'
            ' "auth_scheme": "Bearer",'
            ' "token_env": "EGRESS_PROXY_TOKEN_0"}]}'
        )

    def test_rejects_bad_json(self):
        with self.assertRaises(EgressProxyApplyError) as cm:
            validate_routes_content("{not json")
        self.assertIn("not valid", str(cm.exception))

    def test_rejects_non_object_top_level(self):
        with self.assertRaises(EgressProxyApplyError):
            validate_routes_content("[]")

    def test_rejects_missing_routes_key(self):
        with self.assertRaises(EgressProxyApplyError):
            validate_routes_content('{"other": []}')

    def test_rejects_non_list_routes(self):
        with self.assertRaises(EgressProxyApplyError):
            validate_routes_content('{"routes": "not a list"}')

    def test_rejects_partial_auth_pair(self):
        # The addon-core parser enforces both-or-neither — the apply
        # path picks this up before SIGHUP'ing the sidecar.
        with self.assertRaises(EgressProxyApplyError):
            validate_routes_content(
                '{"routes": [{"host": "x.example",'
                ' "auth_scheme": "Bearer"}]}'
            )


if __name__ == "__main__":
    unittest.main()
