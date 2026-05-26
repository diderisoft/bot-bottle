"""Unit: Egress route lift + routes.yaml render + token
resolution (PRD 0017)."""

import unittest

from claude_bottle.egress import (
    DEFAULT_ALLOWLIST,
    egress_manifest_routes,
    egress_render_routes,
    egress_resolve_token_values,
    egress_routes_for_bottle,
    egress_token_env_map,
)
from claude_bottle.log import Die
from claude_bottle.manifest import Manifest
from claude_bottle.yaml_subset import parse_yaml_subset


def _bottle(routes):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"egress": {"routes": routes}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


class TestRoutesForBottle(unittest.TestCase):
    def test_authenticated_route_gets_slot(self):
        b = _bottle([{
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
        }])
        routes = egress_manifest_routes(b)
        self.assertEqual(1, len(routes))
        r = routes[0]
        self.assertEqual("api.github.com", r.host)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", r.token_env)
        self.assertEqual("GH_PAT", r.token_ref)
        self.assertEqual((), r.path_allowlist)

    def test_unauthenticated_route_has_empty_auth_fields(self):
        b = _bottle([{"host": "github.com", "path_allowlist": ["/x/"]}])
        routes = egress_manifest_routes(b)
        r = routes[0]
        self.assertEqual("", r.auth_scheme)
        self.assertEqual("", r.token_env)
        self.assertEqual("", r.token_ref)
        self.assertEqual(("/x/",), r.path_allowlist)

    def test_shared_token_ref_collapses_to_one_slot(self):
        b = _bottle([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"}},
            {"host": "github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"}},
        ])
        routes = egress_manifest_routes(b)
        slots = {r.token_env for r in routes}
        self.assertEqual({"EGRESS_TOKEN_0"}, slots)

    def test_distinct_token_refs_get_distinct_slots(self):
        b = _bottle([
            {"host": "a.example",
             "auth": {"scheme": "Bearer", "token_ref": "T1"}},
            {"host": "b.example",
             "auth": {"scheme": "Bearer", "token_ref": "T2"}},
        ])
        routes = egress_manifest_routes(b)
        slots = [r.token_env for r in routes]
        self.assertEqual(["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"], slots)

    def test_unauthenticated_routes_dont_consume_slots(self):
        # A bare-pass route between two authenticated routes mustn't
        # skip a slot number — slot 0 + slot 1 stay tight.
        b = _bottle([
            {"host": "a.example",
             "auth": {"scheme": "Bearer", "token_ref": "T1"}},
            {"host": "passthrough.example"},
            {"host": "b.example",
             "auth": {"scheme": "Bearer", "token_ref": "T2"}},
        ])
        routes = egress_manifest_routes(b)
        authed = [r.token_env for r in routes if r.token_env]
        self.assertEqual(["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"], authed)
        self.assertEqual("", routes[1].token_env)


class TestRoutesForBottleFoldsDefaults(unittest.TestCase):
    """The effective route table includes DEFAULT_ALLOWLIST +
    bottle.egress.allowlist as bare-pass entries — pipelock's
    allowlist is a mirror of this set."""

    def test_defaults_present_when_no_manifest_routes(self):
        b = _bottle([])
        hosts = [r.host for r in egress_routes_for_bottle(b)]
        for default in DEFAULT_ALLOWLIST:
            self.assertIn(default, hosts)

    def test_manifest_route_wins_over_default(self):
        # api.anthropic.com is in DEFAULT_ALLOWLIST. A manifest
        # route for the same host takes precedence — we want the
        # auth config to apply, not a duplicate bare-pass entry.
        b = _bottle([{
            "host": "api.anthropic.com",
            "auth": {"scheme": "Bearer", "token_ref": "T"},
        }])
        routes = egress_routes_for_bottle(b)
        anthropic = [r for r in routes if r.host == "api.anthropic.com"]
        self.assertEqual(1, len(anthropic))
        self.assertEqual("Bearer", anthropic[0].auth_scheme)

    def test_manifest_only_when_no_defaults_or_allowlist(self):
        # Sanity: egress_manifest_routes returns just the
        # manifest entries — defaults are added by the
        # _routes_for_bottle wrapper.
        b = _bottle([{"host": "x.example"}])
        manifest = [r.host for r in egress_manifest_routes(b)]
        self.assertEqual(["x.example"], manifest)


class TestTokenEnvMap(unittest.TestCase):
    def test_only_authenticated_routes_contribute(self):
        b = _bottle([
            {"host": "a.example",
             "auth": {"scheme": "Bearer", "token_ref": "T1"}},
            {"host": "passthrough.example"},
        ])
        routes = egress_manifest_routes(b)
        m = egress_token_env_map(routes)
        self.assertEqual({"EGRESS_TOKEN_0": "T1"}, m)

    def test_no_routes_empty(self):
        self.assertEqual({}, egress_token_env_map(()))


class TestRenderRoutes(unittest.TestCase):
    """Render is YAML now (PRD 0017 follow-up). Tests parse the
    output via `yaml_subset` — the same parser the addon uses —
    so the assertions check the actual semantic shape the addon
    will see, not the textual layout."""

    @staticmethod
    def _parsed(routes) -> list[dict]:
        return parse_yaml_subset(egress_render_routes(routes))["routes"]

    def test_authenticated_route_serialised_with_auth_fields(self):
        b = _bottle([{
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
            "path_allowlist": ["/repos/x/"],
        }])
        routes = egress_manifest_routes(b)
        parsed = self._parsed(routes)
        self.assertEqual(
            [{
                "host": "api.github.com",
                "path_allowlist": ["/repos/x/"],
                "auth_scheme": "Bearer",
                "token_env": "EGRESS_TOKEN_0",
            }],
            parsed,
        )

    def test_unauthenticated_route_omits_auth_fields(self):
        # auth_scheme + token_env keys are absent when the route was
        # declared without an `auth` block — the addon's parser
        # enforces both-or-neither, so emitting empty strings would
        # round-trip as a partial pair and crash.
        b = _bottle([{"host": "github.com", "path_allowlist": ["/x/"]}])
        routes = egress_manifest_routes(b)
        entry = self._parsed(routes)[0]
        self.assertNotIn("auth_scheme", entry)
        self.assertNotIn("token_env", entry)

    def test_no_path_allowlist_omits_field(self):
        b = _bottle([{
            "host": "api.anthropic.com",
            "auth": {"scheme": "Bearer", "token_ref": "CL"},
        }])
        routes = egress_manifest_routes(b)
        self.assertNotIn("path_allowlist", self._parsed(routes)[0])

    def test_empty_routes_round_trips(self):
        rendered = egress_render_routes(())
        # Inline-empty-list form is what the parser accepts.
        self.assertEqual([], parse_yaml_subset(rendered)["routes"])

    def test_round_trip_through_addon_core(self):
        # Render here → parse in the addon must succeed for every
        # combination the manifest can produce.
        from claude_bottle.egress_addon_core import load_routes
        b = _bottle([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
             "path_allowlist": ["/repos/x/"]},
            {"host": "github.com", "path_allowlist": ["/x/"]},
            {"host": "api.anthropic.com"},
        ])
        routes = egress_manifest_routes(b)
        addon_routes = load_routes(egress_render_routes(routes))
        self.assertEqual(3, len(addon_routes))
        self.assertEqual("Bearer", addon_routes[0].auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", addon_routes[0].token_env)
        self.assertEqual("", addon_routes[1].auth_scheme)
        self.assertEqual("", addon_routes[2].auth_scheme)


class TestResolveTokenValues(unittest.TestCase):
    def test_reads_host_env(self):
        out = egress_resolve_token_values(
            {"EGRESS_TOKEN_0": "GH_PAT"},
            {"GH_PAT": "the-value"},
        )
        self.assertEqual({"EGRESS_TOKEN_0": "the-value"}, out)

    def test_missing_token_ref_dies(self):
        with self.assertRaises(Die):
            egress_resolve_token_values(
                {"EGRESS_TOKEN_0": "GH_PAT"},
                {},
            )

    def test_empty_token_ref_dies(self):
        with self.assertRaises(Die):
            egress_resolve_token_values(
                {"EGRESS_TOKEN_0": "GH_PAT"},
                {"GH_PAT": ""},
            )


if __name__ == "__main__":
    unittest.main()
