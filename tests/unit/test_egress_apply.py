"""Unit: validate_routes_content (PRD 0014 retargeted by PRD 0017
chunk 3, PRD 0053). docker exec / cp / kill paths are covered by the
integration test."""

import unittest

from bot_bottle.backend.docker.egress_apply import (
    EgressApplyError,
    _merge_single_route,
    validate_routes_content,
)
from bot_bottle.yaml_subset import parse_yaml_subset


_ROUTES_EMPTY = "routes: []\n"
_ROUTES_ONE = 'routes:\n  - host: "api.anthropic.com"\n'


def _routes(parsed: str) -> list[dict]:  # type: ignore
    """Parse a YAML routes string and pull out the routes list, so
    tests can assert on shape directly."""
    return parse_yaml_subset(parsed)["routes"]  # type: ignore


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


class TestMergeSingleRoute(unittest.TestCase):
    BASE = _ROUTES_ONE

    def test_appends_route_when_host_absent(self):
        merged = _merge_single_route(self.BASE, {"host": "github.com"})
        hosts = [r["host"] for r in _routes(merged)]
        self.assertEqual(["api.anthropic.com", "github.com"], hosts)

    def test_appends_matches(self):
        merged = _merge_single_route(
            self.BASE,
            {"host": "github.com", "matches": [
                {"paths": [{"value": "/repos/x/"}]}
            ]},
        )
        new_route = _routes(merged)[-1]
        self.assertIn("matches", new_route)

    def test_appends_legacy_path_allowlist_as_matches(self):
        merged = _merge_single_route(
            self.BASE,
            {"host": "github.com", "path_allowlist": ["/repos/x/"]},
        )
        new_route = _routes(merged)[-1]
        self.assertIn("matches", new_route)

    def test_appends_auth_with_token_env_slot(self):
        merged = _merge_single_route(
            self.BASE,
            {
                "host": "api.github.com",
                "auth": {"scheme": "Bearer", "token_ref": "GH"},
            },
        )
        new_route = _routes(merged)[-1]
        self.assertEqual("Bearer", new_route["auth_scheme"])
        self.assertEqual("EGRESS_TOKEN_0", new_route["token_env"])

    def test_auth_slot_increments_past_existing(self):
        base = (
            'routes:\n'
            '  - host: "api.anthropic.com"\n'
            '    auth_scheme: "Bearer"\n'
            '    token_env: "EGRESS_TOKEN_0"\n'
        )
        merged = _merge_single_route(base, {
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH"},
        })
        new_route = _routes(merged)[-1]
        self.assertEqual("EGRESS_TOKEN_1", new_route["token_env"])

    def test_existing_host_merges_match_paths_as_union(self):
        base = (
            'routes:\n'
            '  - host: "github.com"\n'
            '    matches:\n'
            '      - paths:\n'
            '          - value: "/a/"\n'
        )
        merged = _merge_single_route(base, {
            "host": "github.com",
            "matches": [{"paths": [{"value": "/b/"}]}],
        })
        routes = _routes(merged)
        self.assertEqual(1, len(routes))
        all_paths: list[str] = []
        for me in routes[0].get("matches", []):
            for p in me.get("paths", []):
                all_paths.append(p["value"])
        self.assertIn("/a/", all_paths)
        self.assertIn("/b/", all_paths)

    def test_existing_host_dedup_match_paths(self):
        base = (
            'routes:\n'
            '  - host: "github.com"\n'
            '    matches:\n'
            '      - paths:\n'
            '          - value: "/a/"\n'
        )
        merged = _merge_single_route(base, {
            "host": "github.com",
            "matches": [{"paths": [{"value": "/a/"}, {"value": "/b/"}]}],
        })
        all_paths: list[str] = []
        for me in _routes(merged)[0].get("matches", []):
            for p in me.get("paths", []):
                all_paths.append(p["value"])
        self.assertEqual(1, all_paths.count("/a/"))
        self.assertIn("/b/", all_paths)

    def test_existing_host_preserves_existing_auth_ignores_proposed(self):
        base = (
            'routes:\n'
            '  - host: "api.github.com"\n'
            '    auth_scheme: "Bearer"\n'
            '    token_env: "EGRESS_TOKEN_0"\n'
        )
        merged = _merge_single_route(base, {
            "host": "api.github.com",
            "auth": {"scheme": "token", "token_ref": "DIFFERENT"},
        })
        route = _routes(merged)[0]
        self.assertEqual("Bearer", route["auth_scheme"])
        self.assertEqual("EGRESS_TOKEN_0", route["token_env"])

    def test_host_match_is_case_insensitive(self):
        base = 'routes:\n  - host: "GitHub.com"\n'
        merged = _merge_single_route(base, {
            "host": "github.com",
            "matches": [{"paths": [{"value": "/x/"}]}],
        })
        routes = _routes(merged)
        self.assertEqual(1, len(routes))

    def test_missing_host_raises(self):
        with self.assertRaises(EgressApplyError):
            _merge_single_route(self.BASE, {})

    def test_invalid_current_yaml_raises(self):
        with self.assertRaises(EgressApplyError):
            _merge_single_route("routes:\n\tbad", {"host": "x.example"})


if __name__ == "__main__":
    unittest.main()
