"""Unit: egress_addon_core route parsing, serialization, and match
evaluation error/edge branches (coverage ratchet, ADR 0004).

Complements test_egress_addon_core.py — focuses on the validation
rejections, the Route->YAML serializer, and evaluate_matches."""

from __future__ import annotations

import unittest

from bot_bottle.gateway.egress.addon_core import (
    HeaderMatch,
    MatchEntry,
    PathMatch,
    Route,
    evaluate_matches,
    load_config,
    parse_config,
    parse_routes,
    route_to_yaml_dict,
)


def _route(d: dict[str, object]) -> Route:
    d = _inspect_shape(d)
    return parse_routes({"routes": [d]})[0]

def _inspect_shape(d: dict[str, object]) -> dict[str, object]:
    """Keep legacy test cases compact while exercising the new wire shape."""
    out = dict(d)
    if "dlp" in out:
        dlp = out.pop("dlp")
        if dlp is False:
            out["inspect"] = False
            return out
        out["inspect"] = dlp
    controls = ("matches", "auth_scheme", "token_env", "git", "preserve_auth")
    moved = {key: out.pop(key) for key in controls if key in out}
    if moved:
        inspected: dict[str, object] = dict(
            out.get("inspect", {})  # type: ignore[arg-type]
        )
        inspected.update(moved)
        out["inspect"] = inspected
    return out


class TestRouteValidationErrors(unittest.TestCase):
    def _bad(self, d: dict[str, object]) -> None:
        with self.assertRaises(ValueError):
            parse_routes({"routes": [d]})

    # routes-payload shape
    def test_payload_not_dict(self) -> None:
        with self.assertRaises(ValueError):
            parse_routes(["nope"])

    def test_routes_not_list(self) -> None:
        with self.assertRaises(ValueError):
            parse_routes({"routes": "nope"})

    def test_route_not_dict(self) -> None:
        with self.assertRaises(ValueError):
            parse_routes({"routes": ["nope"]})

    def test_host_missing(self) -> None:
        self._bad({})

    def test_unknown_route_key(self) -> None:
        self._bad({"host": "h", "bogus": 1})

    # auth
    def test_auth_scheme_without_token_env(self) -> None:
        self._bad({"host": "h", "auth_scheme": "Bearer"})

    def test_auth_scheme_wrong_type(self) -> None:
        self._bad({"host": "h", "auth_scheme": 5, "token_env": "T"})

    # git
    def test_git_not_dict(self) -> None:
        self._bad({"host": "h", "git": "yes"})

    def test_git_fetch_not_bool(self) -> None:
        self._bad({"host": "h", "git": {"fetch": "yes"}})

    def test_git_unknown_key(self) -> None:
        self._bad({"host": "h", "git": {"fetch": True, "push": True}})

    # matches: paths
    def test_matches_not_list(self) -> None:
        self._bad({"host": "h", "matches": "x"})

    def test_match_entry_not_dict(self) -> None:
        self._bad({"host": "h", "matches": ["x"]})

    def test_paths_not_list(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": "x"}]})

    def test_path_not_dict(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": ["x"]}]})

    def test_path_bad_type(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": [{"type": "bogus", "value": "/x"}]}]})

    def test_path_empty_value(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": [{"value": ""}]}]})

    def test_path_value_missing_slash(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": [{"type": "prefix", "value": "x"}]}]})

    def test_path_bad_regex(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": [{"type": "regex", "value": "("}]}]})

    def test_path_unknown_key(self) -> None:
        self._bad({"host": "h", "matches": [{"paths": [{"value": "/x", "z": 1}]}]})

    # matches: methods
    def test_methods_not_list(self) -> None:
        self._bad({"host": "h", "matches": [{"methods": "GET"}]})

    def test_method_not_string(self) -> None:
        self._bad({"host": "h", "matches": [{"methods": [5]}]})

    def test_method_invalid(self) -> None:
        self._bad({"host": "h", "matches": [{"methods": ["FETCH"]}]})

    # matches: headers
    def test_headers_not_list(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": "x"}]})

    def test_header_not_dict(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": ["x"]}]})

    def test_header_name_empty(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": [{"name": "", "value": "v"}]}]})

    def test_header_value_not_string(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": [{"name": "X", "value": 1}]}]})

    def test_header_bad_type(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": [{"name": "X", "value": "v", "type": "z"}]}]})

    def test_header_bad_regex(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": [{"name": "X", "value": "(", "type": "regex"}]}]})

    def test_header_unknown_key(self) -> None:
        self._bad({"host": "h", "matches": [{"headers": [{"name": "X", "value": "v", "z": 1}]}]})

    # dlp
    def test_dlp_not_dict(self) -> None:
        self._bad({"host": "h", "dlp": "x"})

    def test_dlp_detectors_wrong_type(self) -> None:
        self._bad({"host": "h", "dlp": {"outbound_detectors": "x"}})

    def test_dlp_detector_name_invalid(self) -> None:
        self._bad({"host": "h", "dlp": {"outbound_detectors": ["bogus"]}})

    def test_dlp_detector_item_not_string(self) -> None:
        self._bad({"host": "h", "dlp": {"outbound_detectors": [5]}})

    def test_dlp_on_match_invalid(self) -> None:
        self._bad({"host": "h", "dlp": {"outbound_on_match": "maybe"}})

    def test_dlp_unknown_key(self) -> None:
        self._bad({"host": "h", "dlp": {"bogus": 1}})


class TestRouteValidAccepts(unittest.TestCase):
    def test_full_route_parses(self) -> None:
        r = _route({
            "host": "api.example.com",
            "auth_scheme": "Bearer",
            "token_env": "TOK",
            "matches": [{
                "paths": [{"type": "exact", "value": "/v1"}],
                "methods": ["get", "post"],
                "headers": [{"name": "X-Env", "value": "prod"}],
            }],
            "git": {"fetch": True},
            "dlp": {
                "outbound_detectors": ["token_patterns"],
                "inbound_detectors": ["naive_injection_detection"],
                "outbound_on_match": "block",
            },
        })
        self.assertEqual("api.example.com", r.host)
        self.assertEqual(("GET", "POST"), r.matches[0].methods)
        self.assertTrue(r.git_fetch)
        self.assertEqual("block", r.outbound_on_match)

    def test_dlp_detectors_false_disables(self) -> None:
        r = _route({"host": "h", "dlp": {"outbound_detectors": False}})
        self.assertEqual((), r.outbound_detectors)

    def test_inspect_false_sets_passthrough(self) -> None:
        r = _route({"host": "h", "inspect": False})
        self.assertFalse(r.inspect)

    def test_inspect_defaults_true(self) -> None:
        r = _route({"host": "h"})
        self.assertTrue(r.inspect)

    def test_inspect_not_a_dict_or_false_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _route({"host": "h", "inspect": "no"})


class TestParseConfig(unittest.TestCase):
    def test_log_must_be_valid_level(self) -> None:
        with self.assertRaises(ValueError):
            parse_config({"log": 5, "routes": []})

    def test_log_true_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_config({"log": True, "routes": []})

    def test_top_level_not_dict(self) -> None:
        with self.assertRaises(ValueError):
            parse_config(["x"])

    def test_load_config_invalid_yaml(self) -> None:
        with self.assertRaises(ValueError):
            load_config("routes: [unterminated\n")


class TestRouteToYamlDict(unittest.TestCase):
    def test_minimal(self) -> None:
        self.assertEqual({"host": "h"}, route_to_yaml_dict(Route(host="h")))

    def test_auth_fields(self) -> None:
        d = route_to_yaml_dict(Route(host="h", auth_scheme="Bearer", token_env="T"))
        self.assertEqual("Bearer", d["inspect"]["auth_scheme"])  # type: ignore[index]
        self.assertEqual("T", d["inspect"]["token_env"])  # type: ignore[index]

    def test_git_fetch(self) -> None:
        d = route_to_yaml_dict(Route(host="h", git_fetch=True))
        self.assertEqual({"fetch": True}, d["inspect"]["git"])  # type: ignore[index]

    def test_dlp_fields(self) -> None:
        d = route_to_yaml_dict(Route(
            host="h",
            outbound_detectors=("token_patterns",),
            inbound_detectors=("naive_injection_detection",),
            outbound_on_match="redact",
        ))
        self.assertEqual(
            {
                "outbound_detectors": ["token_patterns"],
                "inbound_detectors": ["naive_injection_detection"],
                "outbound_on_match": "redact",
            },
            d["inspect"],
        )

    def test_inspect_false_serializes_as_false(self) -> None:
        d = route_to_yaml_dict(Route(host="h", inspect=False))
        self.assertIs(False, d["inspect"])

    def test_inspect_false_roundtrip(self) -> None:
        r = _route({"host": "h", "inspect": False})
        self.assertIs(False, route_to_yaml_dict(r)["inspect"])

    def test_matches_serialization_omits_defaults(self) -> None:
        route = Route(host="h", matches=(MatchEntry(
            paths=(
                PathMatch(type="prefix", value="/p"),   # default type -> omitted
                PathMatch(type="exact", value="/e"),    # non-default -> kept
            ),
            methods=("GET",),
            headers=(
                HeaderMatch(name="X", value="v"),                    # exact -> omitted
                HeaderMatch(name="Y", value="r", type="regex"),      # regex -> kept
            ),
        ),))
        d = route_to_yaml_dict(route)
        matches = d["inspect"]["matches"]  # type: ignore[index]
        assert isinstance(matches, list)
        entry = matches[0]
        self.assertEqual(
            [{"value": "/p"}, {"value": "/e", "type": "exact"}],
            entry["paths"],
        )
        self.assertEqual(["GET"], entry["methods"])
        self.assertEqual(
            [{"name": "X", "value": "v"}, {"name": "Y", "value": "r", "type": "regex"}],
            entry["headers"],
        )


class TestEvaluateMatches(unittest.TestCase):
    def _route_with(self, entry: MatchEntry) -> Route:
        return Route(host="h", matches=(entry,))

    def test_empty_matches_allows_all(self) -> None:
        self.assertTrue(evaluate_matches(Route(host="h"), "/anything", "GET"))

    def test_exact_path(self) -> None:
        r = self._route_with(MatchEntry(paths=(PathMatch("exact", "/a"),)))
        self.assertTrue(evaluate_matches(r, "/a", "GET"))
        self.assertFalse(evaluate_matches(r, "/a/b", "GET"))

    def test_prefix_path_boundary(self) -> None:
        r = self._route_with(MatchEntry(paths=(PathMatch("prefix", "/a"),)))
        self.assertTrue(evaluate_matches(r, "/a/b", "GET"))
        self.assertFalse(evaluate_matches(r, "/ab", "GET"))

    def test_regex_path(self) -> None:
        import re
        r = self._route_with(MatchEntry(
            paths=(PathMatch("regex", r"/v\d+", compiled=re.compile(r"/v\d+")),),
        ))
        self.assertTrue(evaluate_matches(r, "/v1", "GET"))
        self.assertFalse(evaluate_matches(r, "/x", "GET"))

    def test_method_filter(self) -> None:
        r = self._route_with(MatchEntry(methods=("POST",)))
        self.assertTrue(evaluate_matches(r, "/x", "post"))
        self.assertFalse(evaluate_matches(r, "/x", "GET"))

    def test_header_exact(self) -> None:
        r = self._route_with(MatchEntry(headers=(HeaderMatch("X-Env", "prod"),)))
        self.assertTrue(evaluate_matches(r, "/x", "GET", {"x-env": "prod"}))
        self.assertFalse(evaluate_matches(r, "/x", "GET", {"x-env": "dev"}))
        self.assertFalse(evaluate_matches(r, "/x", "GET", {}))

    def test_header_regex(self) -> None:
        import re
        r = self._route_with(MatchEntry(
            headers=(HeaderMatch("X-Env", r"pr.*", type="regex", compiled=re.compile(r"pr.*")),),
        ))
        self.assertTrue(evaluate_matches(r, "/x", "GET", {"x-env": "prod"}))
        self.assertFalse(evaluate_matches(r, "/x", "GET", {"x-env": "dev"}))


if __name__ == "__main__":
    unittest.main()
