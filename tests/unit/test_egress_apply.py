"""Unit: validate_routes_content (PRD 0014 retargeted by PRD 0017
chunk 3). docker exec / cp / kill paths are covered by the
integration test."""

import unittest

from bot_bottle.backend.docker.egress_apply import (
    EgressApplyError,
    _merge_single_route,
    validate_routes_content,
)
from bot_bottle.yaml_subset import parse_yaml_subset


# YAML fixtures matching the hand-rolled `_render_routes_payload`
# shape. Per-test custom shapes are spelled inline; these are the
# common ones.
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

    def test_accepts_full_route(self):
        validate_routes_content(
            'routes:\n'
            '  - host: "api.github.com"\n'
            '    auth_scheme: "Bearer"\n'
            '    token_env: "EGRESS_TOKEN_0"\n'
            '    path_allowlist:\n'
            '      - "/repos/x/"\n'
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
        # The addon-core parser enforces both-or-neither — the apply
        # path picks this up before SIGHUP'ing the sidecar.
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

    def test_appends_path_allowlist(self):
        merged = _merge_single_route(
            self.BASE,
            {"host": "github.com", "path_allowlist": ["/repos/x/"]},
        )
        new_route = _routes(merged)[-1]
        self.assertEqual(["/repos/x/"], new_route["path_allowlist"])

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
        # First auth slot when no prior auth routes exist.
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

    def test_existing_host_merges_path_allowlist_as_union(self):
        base = (
            'routes:\n'
            '  - host: "github.com"\n'
            '    path_allowlist:\n'
            '      - "/a/"\n'
        )
        merged = _merge_single_route(base, {
            "host": "github.com",
            "path_allowlist": ["/b/"],
        })
        routes = _routes(merged)
        self.assertEqual(1, len(routes))  # not duplicated
        self.assertEqual(["/a/", "/b/"], routes[0]["path_allowlist"])

    def test_existing_host_dedup_path_allowlist(self):
        base = (
            'routes:\n'
            '  - host: "github.com"\n'
            '    path_allowlist:\n'
            '      - "/a/"\n'
        )
        merged = _merge_single_route(base, {
            "host": "github.com",
            "path_allowlist": ["/a/", "/b/"],
        })
        self.assertEqual(
            ["/a/", "/b/"],
            _routes(merged)[0]["path_allowlist"],
        )

    def test_existing_host_preserves_existing_auth_ignores_proposed(self):
        # Tool docs: auth on an existing host is operator-controlled,
        # not agent-controlled. The merge must not overwrite.
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
            "path_allowlist": ["/x/"],
        })
        routes = _routes(merged)
        self.assertEqual(1, len(routes))
        self.assertEqual(["/x/"], routes[0]["path_allowlist"])

    def test_missing_host_raises(self):
        with self.assertRaises(EgressApplyError):
            _merge_single_route(self.BASE, {})

    def test_invalid_current_yaml_raises(self):
        with self.assertRaises(EgressApplyError):
            _merge_single_route("routes:\n\tbad", {"host": "x.example"})


if __name__ == "__main__":
    unittest.main()
