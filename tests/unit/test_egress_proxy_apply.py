"""Unit: validate_routes_content (PRD 0014 retargeted by PRD 0017
chunk 3). docker exec / cp / kill paths are covered by the
integration test."""

import unittest

import json

from claude_bottle.backend.docker.egress_proxy_apply import (
    EgressProxyApplyError,
    _hosts_in_routes,
    _merge_single_route,
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


class TestHostsInRoutes(unittest.TestCase):
    def test_extracts_each_unique_host(self):
        hosts = _hosts_in_routes(
            '{"routes": [{"host": "api.github.com"},'
            ' {"host": "github.com"},'
            ' {"host": "api.anthropic.com"}]}'
        )
        # Sorted+deduped.
        self.assertEqual(
            ["api.anthropic.com", "api.github.com", "github.com"],
            hosts,
        )

    def test_dedupes_same_host(self):
        hosts = _hosts_in_routes(
            '{"routes": [{"host": "x.example", "path_allowlist": ["/a/"]},'
            ' {"host": "x.example", "path_allowlist": ["/b/"]}]}'
        )
        self.assertEqual(["x.example"], hosts)

    def test_empty_routes_returns_empty(self):
        self.assertEqual([], _hosts_in_routes('{"routes": []}'))

    def test_invalid_routes_raises(self):
        # The mirror helper relies on parsing succeeding; bad input
        # should error before pipelock is touched.
        with self.assertRaises(EgressProxyApplyError):
            _hosts_in_routes('{"routes": [{"path": "/no-host/"}]}')


class TestMergeSingleRoute(unittest.TestCase):
    BASE = '{"routes": [{"host": "api.anthropic.com"}]}'

    def test_appends_route_when_host_absent(self):
        merged = _merge_single_route(self.BASE, {"host": "github.com"})
        routes = json.loads(merged)["routes"]
        hosts = [r["host"] for r in routes]
        self.assertEqual(["api.anthropic.com", "github.com"], hosts)

    def test_appends_path_allowlist(self):
        merged = _merge_single_route(
            self.BASE,
            {"host": "github.com", "path_allowlist": ["/repos/x/"]},
        )
        new_route = json.loads(merged)["routes"][-1]
        self.assertEqual(["/repos/x/"], new_route["path_allowlist"])

    def test_appends_auth_with_token_env_slot(self):
        merged = _merge_single_route(
            self.BASE,
            {
                "host": "api.github.com",
                "auth": {"scheme": "Bearer", "token_ref": "GH"},
            },
        )
        new_route = json.loads(merged)["routes"][-1]
        self.assertEqual("Bearer", new_route["auth_scheme"])
        # First auth slot when no prior auth routes exist.
        self.assertEqual("EGRESS_PROXY_TOKEN_0", new_route["token_env"])

    def test_auth_slot_increments_past_existing(self):
        base = json.dumps({"routes": [
            {"host": "api.anthropic.com",
             "auth_scheme": "Bearer",
             "token_env": "EGRESS_PROXY_TOKEN_0"},
        ]})
        merged = _merge_single_route(base, {
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH"},
        })
        new_route = json.loads(merged)["routes"][-1]
        self.assertEqual("EGRESS_PROXY_TOKEN_1", new_route["token_env"])

    def test_existing_host_merges_path_allowlist_as_union(self):
        base = json.dumps({"routes": [
            {"host": "github.com", "path_allowlist": ["/a/"]},
        ]})
        merged = _merge_single_route(base, {
            "host": "github.com",
            "path_allowlist": ["/b/"],
        })
        routes = json.loads(merged)["routes"]
        self.assertEqual(1, len(routes))  # not duplicated
        self.assertEqual(["/a/", "/b/"], routes[0]["path_allowlist"])

    def test_existing_host_dedup_path_allowlist(self):
        base = json.dumps({"routes": [
            {"host": "github.com", "path_allowlist": ["/a/"]},
        ]})
        merged = _merge_single_route(base, {
            "host": "github.com",
            "path_allowlist": ["/a/", "/b/"],
        })
        self.assertEqual(
            ["/a/", "/b/"],
            json.loads(merged)["routes"][0]["path_allowlist"],
        )

    def test_existing_host_preserves_existing_auth_ignores_proposed(self):
        # Tool docs: auth on an existing host is operator-controlled,
        # not agent-controlled. The merge must not overwrite.
        base = json.dumps({"routes": [
            {"host": "api.github.com",
             "auth_scheme": "Bearer",
             "token_env": "EGRESS_PROXY_TOKEN_0"},
        ]})
        merged = _merge_single_route(base, {
            "host": "api.github.com",
            "auth": {"scheme": "token", "token_ref": "DIFFERENT"},
        })
        route = json.loads(merged)["routes"][0]
        self.assertEqual("Bearer", route["auth_scheme"])
        self.assertEqual("EGRESS_PROXY_TOKEN_0", route["token_env"])

    def test_host_match_is_case_insensitive(self):
        base = json.dumps({"routes": [{"host": "GitHub.com"}]})
        merged = _merge_single_route(base, {
            "host": "github.com",
            "path_allowlist": ["/x/"],
        })
        routes = json.loads(merged)["routes"]
        self.assertEqual(1, len(routes))
        self.assertEqual(["/x/"], routes[0]["path_allowlist"])

    def test_missing_host_raises(self):
        with self.assertRaises(EgressProxyApplyError):
            _merge_single_route(self.BASE, {})

    def test_invalid_current_yaml_raises(self):
        with self.assertRaises(EgressProxyApplyError):
            _merge_single_route("{not json", {"host": "x.example"})


if __name__ == "__main__":
    unittest.main()
