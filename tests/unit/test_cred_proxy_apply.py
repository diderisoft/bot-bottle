"""Unit: validate_routes_json (PRD 0014 Phase 2). docker exec / cp /
kill paths are covered by the integration test."""

import unittest

from claude_bottle.backend.docker.cred_proxy_apply import (
    CredProxyApplyError,
    validate_routes_json,
)


class TestValidateRoutesJson(unittest.TestCase):
    def test_accepts_routes_array(self):
        validate_routes_json('{"routes": []}')
        validate_routes_json(
            '{"routes": [{"path": "/a/", "upstream": "https://example.com",'
            ' "auth_scheme": "Bearer", "token_env": "T0"}]}'
        )

    def test_rejects_bad_json(self):
        with self.assertRaises(CredProxyApplyError) as cm:
            validate_routes_json("{not json")
        self.assertIn("not valid JSON", str(cm.exception))

    def test_rejects_non_object_top_level(self):
        with self.assertRaises(CredProxyApplyError):
            validate_routes_json("[]")

    def test_rejects_missing_routes_key(self):
        with self.assertRaises(CredProxyApplyError):
            validate_routes_json('{"other": []}')

    def test_rejects_non_list_routes(self):
        with self.assertRaises(CredProxyApplyError):
            validate_routes_json('{"routes": "not a list"}')


if __name__ == "__main__":
    unittest.main()
