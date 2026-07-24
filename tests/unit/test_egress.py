"""Unit: Egress route lift + routes.yaml render + token
resolution (PRD 0017, PRD 0053)."""

import tempfile
import unittest
from pathlib import Path

from bot_bottle.egress import (
    CODEX_HOST_CREDENTIAL_TOKEN_REF,
    Egress,
    EgressPlan,
    EgressRoute,
    _yaml_str_escape,
    egress_agent_env_entries,
    egress_manifest_routes,
    egress_render_routes,
    egress_resolve_token_values,
    egress_routes_for_bottle,
    egress_gateway_env_entries,
    egress_token_env_map,
)
from bot_bottle.errors import MissingEnvVarError
from bot_bottle.manifest import ManifestIndex
from bot_bottle.yaml_subset import parse_yaml_subset


def _inspect_routes(routes):  # type: ignore
    out = []
    for route in routes:
        route = dict(route)
        if "dlp" in route:
            dlp = route.pop("dlp")
            if dlp is False:
                route["inspect"] = False
                out.append(route)
                continue
            route["inspect"] = dlp
        controls = ("matches", "auth", "git", "preserve_auth")
        moved = {key: route.pop(key) for key in controls if key in route}
        if moved:
            inspected = dict(route.get("inspect", {}))
            inspected.update(moved)
            route["inspect"] = inspected
        out.append(route)
    return out


def _bottle(routes):  # type: ignore
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": {"egress": {"routes": _inspect_routes(routes)}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _provider_route(host: str, token_ref: str) -> EgressRoute:
    return EgressRoute(
        host=host,
        auth_scheme="Bearer",
        token_ref=token_ref,
    )


class TestManifestRouteLift(unittest.TestCase):
    """egress_manifest_routes is a pure lifter — no slot assignment."""

    def test_authenticated_route_lifted_without_slot(self):
        b = _bottle([{
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
        }])
        routes = egress_manifest_routes(b)
        self.assertEqual(1, len(routes))
        r = routes[0]
        self.assertEqual("api.github.com", r.host)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("GH_PAT", r.token_ref)
        self.assertEqual("", r.token_env)
        self.assertEqual((), r.matches)

    def test_unauthenticated_route_has_empty_auth_fields(self):
        b = _bottle([{"host": "github.com", "matches": [
            {"paths": [{"value": "/x/"}]}
        ]}])
        routes = egress_manifest_routes(b)
        r = routes[0]
        self.assertEqual("", r.auth_scheme)
        self.assertEqual("", r.token_env)
        self.assertEqual("", r.token_ref)
        self.assertEqual(1, len(r.matches))
        self.assertEqual(1, len(r.matches[0].paths))
        self.assertEqual("/x/", r.matches[0].paths[0].value)

    def test_matches_with_methods_and_headers(self):
        b = _bottle([{"host": "api.example.com", "matches": [
            {
                "paths": [{"value": "/api/"}],
                "methods": ["GET", "POST"],
                "headers": [{"name": "content-type", "value": "application/json"}],
            }
        ]}])
        routes = egress_manifest_routes(b)
        m = routes[0].matches[0]
        self.assertEqual(("GET", "POST"), m.methods)
        self.assertEqual(1, len(m.headers))
        self.assertEqual("content-type", m.headers[0].name)

    def test_dlp_detectors_lifted(self):
        b = _bottle([{"host": "x.example", "dlp": {
            "outbound_detectors": ["token_patterns"],
            "inbound_detectors": False,
        }}])
        routes = egress_manifest_routes(b)
        r = routes[0]
        self.assertEqual(("token_patterns",), r.outbound_detectors)
        self.assertEqual((), r.inbound_detectors)

    def test_git_fetch_policy_lifted(self):
        b = _bottle([{"host": "github.com", "git": {"fetch": True}}])
        routes = egress_manifest_routes(b)
        self.assertTrue(routes[0].git_fetch)


class TestSlotAssignment(unittest.TestCase):
    """Slot assignment happens in egress_routes_for_bottle."""

    def test_authenticated_route_gets_slot(self):
        b = _bottle([{
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
        }])
        routes = egress_routes_for_bottle(b)
        r = routes[0]
        self.assertEqual("EGRESS_TOKEN_0", r.token_env)
        self.assertEqual("GH_PAT", r.token_ref)

    def test_shared_token_ref_collapses_to_one_slot(self):
        b = _bottle([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"}},
            {"host": "github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"}},
        ])
        routes = egress_routes_for_bottle(b)
        slots = {r.token_env for r in routes}
        self.assertEqual({"EGRESS_TOKEN_0"}, slots)

    def test_distinct_token_refs_get_distinct_slots(self):
        b = _bottle([
            {"host": "a.example",
             "auth": {"scheme": "Bearer", "token_ref": "T1"}},
            {"host": "b.example",
             "auth": {"scheme": "Bearer", "token_ref": "T2"}},
        ])
        routes = egress_routes_for_bottle(b)
        slots = [r.token_env for r in routes]
        self.assertEqual(["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"], slots)

    def test_unauthenticated_routes_dont_consume_slots(self):
        b = _bottle([
            {"host": "a.example",
             "auth": {"scheme": "Bearer", "token_ref": "T1"}},
            {"host": "passthrough.example"},
            {"host": "b.example",
             "auth": {"scheme": "Bearer", "token_ref": "T2"}},
        ])
        routes = egress_routes_for_bottle(b)
        authed = [r.token_env for r in routes if r.token_env]
        self.assertEqual(["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"], authed)
        self.assertEqual("", routes[1].token_env)


class TestRoutesForBottleManifestOnly(unittest.TestCase):
    """Without provider routes the effective table is exactly the manifest."""

    def test_no_manifest_routes_means_no_effective_routes(self):
        b = _bottle([])
        self.assertEqual((), egress_routes_for_bottle(b))

    def test_manifest_route_preserved_with_auth(self):
        b = _bottle([{
            "host": "api.anthropic.com",
            "auth": {"scheme": "Bearer", "token_ref": "T"},
        }])
        routes = egress_routes_for_bottle(b)
        self.assertEqual(1, len(routes))
        self.assertEqual("api.anthropic.com", routes[0].host)
        self.assertEqual("Bearer", routes[0].auth_scheme)

    def test_manifest_only(self):
        b = _bottle([{"host": "x.example"}])
        effective = [r.host for r in egress_routes_for_bottle(b)]
        self.assertEqual(["x.example"], effective)



class TestProviderRouteMerge(unittest.TestCase):
    """Provider routes win on host collision; manifest fills the rest."""

    def test_provider_route_appended_when_not_in_manifest(self):
        b = _bottle([])
        pr = _provider_route("api.openai.com", "TOK")
        routes = egress_routes_for_bottle(b, (pr,))
        self.assertEqual(1, len(routes))
        self.assertEqual("api.openai.com", routes[0].host)
        self.assertEqual("Bearer", routes[0].auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", routes[0].token_env)
        self.assertEqual("TOK", routes[0].token_ref)

    def test_unauthenticated_provider_route_appends_without_token_slot(self):
        b = _bottle([])
        pr = EgressRoute(host="api.openai.com")
        routes = egress_routes_for_bottle(b, (pr,))
        self.assertEqual(1, len(routes))
        self.assertEqual("api.openai.com", routes[0].host)
        self.assertEqual("", routes[0].auth_scheme)
        self.assertEqual("", routes[0].token_env)
        self.assertEqual("", routes[0].token_ref)
        self.assertEqual({}, egress_token_env_map(routes))

    def test_provider_route_wins_over_bare_manifest_route(self):
        b = _bottle([{"host": "api.openai.com", "matches": [
            {"paths": [{"value": "/v1/"}]}
        ]}])
        pr = EgressRoute(host="api.openai.com")
        routes = egress_routes_for_bottle(b, (pr,))
        self.assertEqual(1, len(routes))
        self.assertEqual("", routes[0].auth_scheme)
        self.assertEqual("", routes[0].token_env)
        self.assertEqual("", routes[0].token_ref)
        self.assertEqual((), routes[0].matches)
        self.assertEqual({}, egress_token_env_map(routes))

    def test_provider_route_defaults_to_redact_on_match(self):
        b = _bottle([])
        pr = EgressRoute(host="api.anthropic.com")
        routes = egress_routes_for_bottle(b, (pr,))
        self.assertEqual("redact", routes[0].outbound_on_match)

    def test_provider_route_explicit_on_match_preserved(self):
        b = _bottle([])
        pr = EgressRoute(host="api.anthropic.com", outbound_on_match="supervise")
        routes = egress_routes_for_bottle(b, (pr,))
        self.assertEqual("supervise", routes[0].outbound_on_match)

    def test_manifest_route_does_not_get_redact_default(self):
        b = _bottle([{"host": "api.example.com"}])
        routes = egress_routes_for_bottle(b)
        self.assertEqual("", routes[0].outbound_on_match)

    def test_two_provider_routes_with_same_token_ref_share_slot(self):
        b = _bottle([])
        routes = egress_routes_for_bottle(b, (
            _provider_route("api.openai.com", CODEX_HOST_CREDENTIAL_TOKEN_REF),
            _provider_route("chatgpt.com",    CODEX_HOST_CREDENTIAL_TOKEN_REF),
        ))
        self.assertEqual(["api.openai.com", "chatgpt.com"], [r.host for r in routes])
        self.assertEqual("EGRESS_TOKEN_0", routes[0].token_env)
        self.assertEqual("EGRESS_TOKEN_0", routes[1].token_env)

    def test_provider_route_wins_over_authed_manifest_route(self):
        b = _bottle([{"host": "chatgpt.com",
                      "matches": [{"paths": [{"value": "/backend-api/"}]}],
                      "auth": {"scheme": "Bearer", "token_ref": "OTHER"}}])
        pr = _provider_route("chatgpt.com", CODEX_HOST_CREDENTIAL_TOKEN_REF)
        routes = egress_routes_for_bottle(b, (pr,))
        self.assertEqual(1, len(routes))
        self.assertEqual("chatgpt.com", routes[0].host)
        self.assertEqual("Bearer", routes[0].auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", routes[0].token_env)
        self.assertEqual(CODEX_HOST_CREDENTIAL_TOKEN_REF, routes[0].token_ref)
        self.assertEqual((), routes[0].matches)

    def test_manifest_route_preserved_for_non_provisioned_host(self):
        b = _bottle([
            {"host": "api.openai.com"},
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"}},
        ])
        pr = _provider_route("api.openai.com", CODEX_HOST_CREDENTIAL_TOKEN_REF)
        routes = egress_routes_for_bottle(b, (pr,))
        hosts = [r.host for r in routes]
        self.assertEqual(["api.openai.com", "api.github.com"], hosts)
        self.assertEqual(CODEX_HOST_CREDENTIAL_TOKEN_REF, routes[0].token_ref)
        self.assertEqual("GH_PAT", routes[1].token_ref)

class TestTokenEnvMap(unittest.TestCase):
    def test_only_authenticated_routes_contribute(self):
        b = _bottle([
            {"host": "a.example",
             "auth": {"scheme": "Bearer", "token_ref": "T1"}},
            {"host": "passthrough.example"},
        ])
        routes = egress_routes_for_bottle(b)
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
    def _parsed(routes) -> list[dict]:  # type: ignore
        return parse_yaml_subset(egress_render_routes(routes))["routes"]  # type: ignore

    def test_authenticated_route_serialised_with_auth_fields(self):
        b = _bottle([{
            "host": "api.github.com",
            "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
            "matches": [{"paths": [{"value": "/repos/x/"}]}],
        }])
        routes = egress_routes_for_bottle(b)
        parsed = self._parsed(routes)
        self.assertEqual(1, len(parsed))
        self.assertEqual("api.github.com", parsed[0]["host"])
        self.assertEqual("Bearer", parsed[0]["inspect"]["auth_scheme"])
        self.assertEqual("EGRESS_TOKEN_0", parsed[0]["inspect"]["token_env"])
        self.assertIn("matches", parsed[0]["inspect"])

    def test_unauthenticated_route_omits_auth_fields(self):
        b = _bottle([{"host": "github.com", "matches": [
            {"paths": [{"value": "/x/"}]}
        ]}])
        routes = egress_routes_for_bottle(b)
        entry = self._parsed(routes)[0]
        self.assertNotIn("auth_scheme", entry["inspect"])
        self.assertNotIn("token_env", entry["inspect"])

    def test_no_matches_omits_field(self):
        b = _bottle([{
            "host": "api.anthropic.com",
            "auth": {"scheme": "Bearer", "token_ref": "CL"},
        }])
        routes = egress_routes_for_bottle(b)
        self.assertNotIn("matches", self._parsed(routes)[0]["inspect"])

    def test_empty_routes_round_trips(self):
        rendered = egress_render_routes(())
        self.assertEqual([], parse_yaml_subset(rendered)["routes"])

    def test_round_trip_through_addon_core(self):
        from bot_bottle.gateway.egress_addon_core import load_config
        b = _bottle([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH_PAT"},
             "matches": [{"paths": [{"value": "/repos/x/"}]}]},
            {"host": "github.com", "matches": [
                {"paths": [{"value": "/x/"}]}
            ]},
            {"host": "api.anthropic.com"},
        ])
        routes = egress_routes_for_bottle(b)
        addon_routes = load_config(egress_render_routes(routes)).routes
        self.assertEqual(3, len(addon_routes))
        self.assertEqual("Bearer", addon_routes[0].auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", addon_routes[0].token_env)
        self.assertEqual("", addon_routes[1].auth_scheme)
        self.assertEqual("", addon_routes[2].auth_scheme)

    def test_dlp_round_trips(self):
        from bot_bottle.gateway.egress_addon_core import load_config
        b = _bottle([{"host": "x.example", "dlp": {
            "outbound_detectors": ["token_patterns"],
            "inbound_detectors": False,
        }}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes)
        addon_routes = load_config(rendered).routes
        self.assertEqual(("token_patterns",), addon_routes[0].outbound_detectors)
        self.assertEqual((), addon_routes[0].inbound_detectors)

    def test_outbound_on_match_round_trips(self):
        from bot_bottle.gateway.egress_addon_core import load_config
        b = _bottle([{"host": "logs.example", "dlp": {
            "outbound_on_match": "redact",
        }}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes)
        self.assertIn('outbound_on_match: "redact"', rendered)
        addon_routes = load_config(rendered).routes
        self.assertEqual("redact", addon_routes[0].outbound_on_match)

    def test_outbound_on_match_default_omitted_from_render(self):
        b = _bottle([{"host": "x.example"}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes)
        self.assertNotIn("outbound_on_match", rendered)

    def test_git_fetch_policy_round_trips(self):
        from bot_bottle.gateway.egress_addon_core import load_config
        b = _bottle([{"host": "github.com", "git": {"fetch": True}}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes)
        self.assertEqual({"fetch": True}, self._parsed(routes)[0]["inspect"]["git"])
        addon_routes = load_config(rendered).routes
        self.assertTrue(addon_routes[0].git_fetch)

    def test_preserve_auth_round_trips_to_the_addon(self):
        """Regression: the manifest parsed preserve_auth and the addon honored
        it, but the renderer in between dropped it — so the flag never reached
        the proxy and registry pulls kept failing with "unauthorized" while
        the config looked correct everywhere it was inspected."""
        from bot_bottle.gateway.egress_addon_core import load_config
        b = _bottle([{"host": "registry-1.docker.io", "preserve_auth": True}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes)
        self.assertIn("preserve_auth: true", rendered)
        self.assertTrue(load_config(rendered).routes[0].preserve_auth)

    def test_preserve_auth_omitted_when_unset(self):
        b = _bottle([{"host": "x.example"}])
        rendered = egress_render_routes(egress_routes_for_bottle(b))
        self.assertNotIn("preserve_auth", rendered)
        from bot_bottle.gateway.egress_addon_core import load_config
        self.assertFalse(load_config(rendered).routes[0].preserve_auth)

    def test_log_zero_omitted_from_render(self):
        b = _bottle([{"host": "x.example"}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes, log=0)
        self.assertNotIn("log:", rendered)

    def test_log_level_emitted_at_top_level(self):
        b = _bottle([{"host": "x.example"}])
        routes = egress_routes_for_bottle(b)
        for level in (1, 2):
            with self.subTest(level=level):
                rendered = egress_render_routes(routes, log=level)
                self.assertTrue(rendered.startswith(f"log: {level}\n"))

    def test_log_level_round_trips_to_addon_core(self):
        from bot_bottle.gateway.egress_addon_core import load_config, LOG_FULL
        b = _bottle([{"host": "x.example"}])
        routes = egress_routes_for_bottle(b)
        rendered = egress_render_routes(routes, log=LOG_FULL)
        cfg = load_config(rendered)
        self.assertEqual(LOG_FULL, cfg.log)
        self.assertEqual("x.example", cfg.routes[0].host)

    def test_log_via_manifest_flows_to_render(self):
        from bot_bottle.manifest import ManifestIndex
        from bot_bottle.gateway.egress_addon_core import load_config, LOG_BLOCKS
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"egress": {
                "log": 1,
                "routes": [{"host": "x.example"}],
            }}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        bottle = m.bottles["dev"]
        self.assertEqual(LOG_BLOCKS, bottle.egress.Log)
        routes = egress_routes_for_bottle(bottle)
        rendered = egress_render_routes(routes, log=bottle.egress.Log)
        cfg = load_config(rendered)
        self.assertEqual(LOG_BLOCKS, cfg.log)


class TestYamlStrEscape(unittest.TestCase):
    """_yaml_str_escape produces safe YAML double-quoted scalar content."""

    def test_plain_string_unchanged(self):
        self.assertEqual("api.example.com", _yaml_str_escape("api.example.com"))

    def test_double_quote_escaped(self):
        self.assertEqual('\\"', _yaml_str_escape('"'))

    def test_backslash_escaped(self):
        self.assertEqual("\\\\", _yaml_str_escape("\\"))

    def test_newline_escaped(self):
        self.assertEqual("\\n", _yaml_str_escape("\n"))

    def test_carriage_return_escaped(self):
        self.assertEqual("\\r", _yaml_str_escape("\r"))

    def test_tab_escaped(self):
        self.assertEqual("\\t", _yaml_str_escape("\t"))

    def test_combined(self):
        self.assertEqual('\\"\\n\\\\', _yaml_str_escape('"\n\\'))


class TestRenderRoutesEscaping(unittest.TestCase):
    """Stray quotes/newlines in manifest strings do not corrupt routes.yaml."""

    @staticmethod
    def _parsed(routes) -> list[dict]:  # type: ignore
        return parse_yaml_subset(egress_render_routes(routes))["routes"]  # type: ignore

    def test_host_with_double_quote_round_trips(self):
        routes = (EgressRoute(host='bad"host.example'),)
        parsed = self._parsed(routes)
        self.assertEqual('bad"host.example', parsed[0]["host"])

    def test_host_with_newline_round_trips(self):
        routes = (EgressRoute(host="host\nextra.example"),)
        parsed = self._parsed(routes)
        self.assertEqual("host\nextra.example", parsed[0]["host"])

    def test_auth_scheme_with_double_quote_round_trips(self):
        routes = (EgressRoute(
            host="api.example",
            auth_scheme='Bear"er',
            token_env="EGRESS_TOKEN_0",
        ),)
        parsed = self._parsed(routes)
        self.assertEqual('Bear"er', parsed[0]["inspect"]["auth_scheme"])

    def test_path_value_with_double_quote_round_trips(self):
        from bot_bottle.gateway.egress_addon_core import PathMatch, MatchEntry
        routes = (EgressRoute(
            host="api.example",
            matches=(MatchEntry(paths=(PathMatch(type="prefix", value='/v1/"quoted"/'),)),),
        ),)
        parsed = self._parsed(routes)
        self.assertEqual('/v1/"quoted"/', parsed[0]["inspect"]["matches"][0]["paths"][0]["value"])

    def test_header_value_with_double_quote_round_trips(self):
        from bot_bottle.gateway.egress_addon_core import HeaderMatch, MatchEntry
        routes = (EgressRoute(
            host="api.example",
            matches=(MatchEntry(headers=(HeaderMatch(name="x-h", value='val"ue'),)),),
        ),)
        parsed = self._parsed(routes)
        self.assertEqual('val"ue', parsed[0]["inspect"]["matches"][0]["headers"][0]["value"])


class TestResolveTokenValues(unittest.TestCase):
    def test_reads_host_env(self):
        out = egress_resolve_token_values(
            {"EGRESS_TOKEN_0": "GH_PAT"},
            {"GH_PAT": "the-value"},
        )
        self.assertEqual({"EGRESS_TOKEN_0": "the-value"}, out)

    def test_missing_token_ref_dies(self):
        with self.assertRaises(MissingEnvVarError):
            egress_resolve_token_values(
                {"EGRESS_TOKEN_0": "GH_PAT"},
                {},
            )

    def test_empty_token_ref_dies(self):
        with self.assertRaises(MissingEnvVarError):
            egress_resolve_token_values(
                {"EGRESS_TOKEN_0": "GH_PAT"},
                {"GH_PAT": ""},
            )

    def test_codex_host_credential_ref_resolved_via_provisioned_env(self):
        out = egress_resolve_token_values(
            {"EGRESS_TOKEN_0": CODEX_HOST_CREDENTIAL_TOKEN_REF},
            {CODEX_HOST_CREDENTIAL_TOKEN_REF: "codex-access-token"},
        )
        self.assertEqual({"EGRESS_TOKEN_0": "codex-access-token"}, out)


class TestCanaryGeneration(unittest.TestCase):
    """Egress.prepare() generates a unique canary token per session."""

    def _bottle_obj(self):
        return ManifestIndex.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": []}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]

    def _make_plan(self) -> EgressPlan:
        # Use a concrete no-op subclass so we can call prepare() without
        # a real backend.
        class _TestEgress(Egress):
            pass

        e = _TestEgress()
        with tempfile.TemporaryDirectory() as td:
            return e.prepare(self._bottle_obj(), "test-slug", Path(td))

    def test_canary_is_non_empty(self):
        plan = self._make_plan()
        self.assertIsInstance(plan.canary, str)
        self.assertGreater(len(plan.canary), 0)
        self.assertRegex(plan.canary_env, r"^[A-Z]+_[A-Z]+_SECRET$")

    def test_canary_is_unique_per_session(self):
        with tempfile.TemporaryDirectory() as td:
            bottle = self._bottle_obj()

            class _TestEgress(Egress):
                pass

            e = _TestEgress()
            plan_a = e.prepare(bottle, "slug-a", Path(td))
            plan_b = e.prepare(bottle, "slug-b", Path(td))
        self.assertNotEqual(plan_a.canary, plan_b.canary)

    def test_canary_detected_by_scan_known_secrets(self):
        from bot_bottle.gateway.dlp_detectors import scan_known_secrets

        plan = self._make_plan()
        env = {plan.canary_env: plan.canary}
        result = scan_known_secrets(
            f"exfil={plan.canary}",
            env=env,
            sensitive_prefixes=(plan.canary_env,),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual("block", result.severity)
        self.assertIn(plan.canary_env, result.reason)

    def test_egress_plan_canary_field_default_empty(self):
        # Verify EgressPlan can be constructed with an empty canary (backward compat).
        from pathlib import Path
        plan = EgressPlan(
            slug="s",
            routes_path=Path("/tmp/r.yaml"),
            routes=(),
            token_env_map={},
        )
        self.assertEqual("", plan.canary)
        self.assertEqual("", plan.canary_env)


class TestEgressEnvEntries(unittest.TestCase):
    def test_gateway_entries_include_route_tokens_and_canary_scan_prefix(self):
        plan = EgressPlan(
            slug="s",
            routes_path=Path("/tmp/r.yaml"),
            routes=(EgressRoute(host="api.example"),),
            token_env_map={"EGRESS_TOKEN_1": "T1", "EGRESS_TOKEN_0": "T0"},
            canary="fake-canary-value",
            canary_env="CANON_ALPHA_SECRET",
        )

        self.assertEqual(
            (
                "EGRESS_TOKEN_0",
                "EGRESS_TOKEN_1",
                "CANON_ALPHA_SECRET=fake-canary-value",
                "BOT_BOTTLE_SENSITIVE_PREFIXES=CANON_ALPHA_SECRET",
            ),
            egress_gateway_env_entries(plan),
        )

    def test_agent_entries_include_only_canary_bait(self):
        plan = EgressPlan(
            slug="s",
            routes_path=Path("/tmp/r.yaml"),
            routes=(),
            token_env_map={},
            canary="fake-canary-value",
            canary_env="CANON_ALPHA_SECRET",
        )

        self.assertEqual(
            ("CANON_ALPHA_SECRET=fake-canary-value",),
            egress_agent_env_entries(plan),
        )

    def test_canary_entries_omitted_when_name_missing(self):
        plan = EgressPlan(
            slug="s",
            routes_path=Path("/tmp/r.yaml"),
            routes=(),
            token_env_map={},
            canary="fake-canary-value",
        )

        self.assertEqual((), egress_gateway_env_entries(plan))
        self.assertEqual((), egress_agent_env_entries(plan))


if __name__ == "__main__":
    unittest.main()
