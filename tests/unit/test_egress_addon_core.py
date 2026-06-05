"""Unit: pure-logic core of the egress mitmproxy addon (PRD 0017, PRD 0053).

These tests target `egress_addon_core` — the host-importable
half of the addon."""

import http.server
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from bot_bottle.egress_addon_core import (
    Decision,
    HeaderMatch,
    MatchEntry,
    PathMatch,
    Route,
    decide,
    evaluate_matches,
    is_git_push_request,
    load_routes,
    match_route,
    parse_routes,
)


# --- parse_routes --------------------------------------------------------


class TestParseRoutes(unittest.TestCase):
    def test_minimal_route(self):
        routes = parse_routes({"routes": [{"host": "api.github.com"}]})
        self.assertEqual(1, len(routes))
        self.assertEqual("api.github.com", routes[0].host)
        self.assertEqual((), routes[0].matches)
        self.assertEqual("", routes[0].auth_scheme)
        self.assertEqual("", routes[0].token_env)

    def test_full_route(self):
        routes = parse_routes({"routes": [{
            "host": "api.github.com",
            "matches": [
                {"paths": [{"type": "prefix", "value": "/repos/x/"}]},
            ],
            "auth_scheme": "Bearer",
            "token_env": "EGRESS_TOKEN_0",
        }]})
        r = routes[0]
        self.assertEqual(1, len(r.matches))
        self.assertEqual(1, len(r.matches[0].paths))
        self.assertEqual("prefix", r.matches[0].paths[0].type)
        self.assertEqual("/repos/x/", r.matches[0].paths[0].value)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", r.token_env)

    def test_order_preserved(self):
        routes = parse_routes({"routes": [
            {"host": "a.example"},
            {"host": "b.example"},
            {"host": "c.example"},
        ]})
        self.assertEqual(
            ["a.example", "b.example", "c.example"],
            [r.host for r in routes],
        )

    def test_partial_auth_pair_rejected(self):
        with self.assertRaises(ValueError) as cm:
            parse_routes({"routes": [{
                "host": "x.example",
                "auth_scheme": "Bearer",
            }]})
        self.assertIn("both set or both empty", str(cm.exception))

    def test_partial_auth_other_direction_rejected(self):
        with self.assertRaises(ValueError) as cm:
            parse_routes({"routes": [{
                "host": "x.example",
                "token_env": "EGRESS_TOKEN_0",
            }]})
        self.assertIn("both set or both empty", str(cm.exception))

    def test_top_level_must_be_object(self):
        with self.assertRaises(ValueError):
            parse_routes(["not", "an", "object"])

    def test_routes_must_be_list(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": "not a list"})

    def test_route_must_have_host(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{}]})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "path_allowlist": ["/x/"],
            }]})


class TestParseMatchEntries(unittest.TestCase):
    def test_path_prefix_default_type(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "matches": [{"paths": [{"value": "/api/"}]}],
        }]})
        self.assertEqual("prefix", routes[0].matches[0].paths[0].type)

    def test_path_exact(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "matches": [{"paths": [{"type": "exact", "value": "/health"}]}],
        }]})
        self.assertEqual("exact", routes[0].matches[0].paths[0].type)

    def test_path_regex(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "matches": [{"paths": [{"type": "regex", "value": "^/v[0-9]+/"}]}],
        }]})
        pm = routes[0].matches[0].paths[0]
        self.assertEqual("regex", pm.type)
        self.assertIsNotNone(pm.compiled)

    def test_path_bad_regex_rejected(self):
        with self.assertRaises(ValueError) as cm:
            parse_routes({"routes": [{
                "host": "x.example",
                "matches": [{"paths": [{"type": "regex", "value": "[bad"}]}],
            }]})
        self.assertIn("failed to compile", str(cm.exception))

    def test_path_prefix_must_start_with_slash(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "matches": [{"paths": [{"value": "no-slash"}]}],
            }]})

    def test_methods_case_insensitive(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "matches": [{"methods": ["get", "Post"]}],
        }]})
        self.assertEqual(("GET", "POST"), routes[0].matches[0].methods)

    def test_invalid_method_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "matches": [{"methods": ["BOGUS"]}],
            }]})

    def test_headers_exact_default(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "matches": [{"headers": [
                {"name": "Content-Type", "value": "application/json"},
            ]}],
        }]})
        hm = routes[0].matches[0].headers[0]
        self.assertEqual("Content-Type", hm.name)
        self.assertEqual("application/json", hm.value)
        self.assertEqual("exact", hm.type)

    def test_headers_regex(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "matches": [{"headers": [
                {"name": "Accept", "value": "application/.*", "type": "regex"},
            ]}],
        }]})
        hm = routes[0].matches[0].headers[0]
        self.assertEqual("regex", hm.type)
        self.assertIsNotNone(hm.compiled)

    def test_unknown_match_key_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "matches": [{"paths": [], "bogus": True}],
            }]})


class TestParseDlp(unittest.TestCase):
    def test_dlp_omitted_means_all_enabled(self):
        routes = parse_routes({"routes": [{"host": "x.example"}]})
        self.assertIsNone(routes[0].outbound_detectors)
        self.assertIsNone(routes[0].inbound_detectors)

    def test_dlp_false_disables(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "dlp": {
                "outbound_detectors": False,
                "inbound_detectors": False,
            },
        }]})
        self.assertEqual((), routes[0].outbound_detectors)
        self.assertEqual((), routes[0].inbound_detectors)

    def test_dlp_named_detectors(self):
        routes = parse_routes({"routes": [{
            "host": "x.example",
            "dlp": {
                "outbound_detectors": ["token_patterns"],
                "inbound_detectors": ["naive_injection_detection"],
            },
        }]})
        self.assertEqual(("token_patterns",), routes[0].outbound_detectors)
        self.assertEqual(("naive_injection_detection",), routes[0].inbound_detectors)

    def test_dlp_unknown_detector_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "dlp": {"outbound_detectors": ["bogus"]},
            }]})

    def test_dlp_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "dlp": {"wat": True},
            }]})


# --- load_routes ---------------------------------------------------------


class TestLoadRoutes(unittest.TestCase):
    def test_yaml_text_round_trip(self):
        routes = load_routes(
            'routes:\n'
            '  - host: "api.example"\n'
        )
        self.assertEqual(1, len(routes))
        self.assertEqual("api.example", routes[0].host)

    def test_full_route_shape_parses(self):
        routes = load_routes(
            'routes:\n'
            '  - host: "api.example"\n'
            '    auth_scheme: "Bearer"\n'
            '    token_env: "EGRESS_TOKEN_0"\n'
            '    matches:\n'
            '      - paths:\n'
            '          - value: "/v1/"\n'
            '          - type: "exact"\n'
            '            value: "/messages"\n'
        )
        self.assertEqual(1, len(routes))
        r = routes[0]
        self.assertEqual("api.example", r.host)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", r.token_env)
        self.assertEqual(1, len(r.matches))
        self.assertEqual(2, len(r.matches[0].paths))

    def test_empty_routes_list(self):
        routes = load_routes("routes: []\n")
        self.assertEqual((), routes)

    def test_invalid_yaml_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_routes("routes:\n\t- host: x\n")


# --- evaluate_matches ---------------------------------------------------


class TestEvaluateMatches(unittest.TestCase):
    def test_empty_matches_allows_all(self):
        route = Route(host="x.example")
        self.assertTrue(evaluate_matches(route, "/anything"))

    def test_prefix_match(self):
        route = Route(host="x.example", matches=(
            MatchEntry(paths=(PathMatch(type="prefix", value="/api/v1"),)),
        ))
        self.assertTrue(evaluate_matches(route, "/api/v1/foo"))
        self.assertTrue(evaluate_matches(route, "/api/v1"))
        self.assertFalse(evaluate_matches(route, "/api/v10"))
        self.assertFalse(evaluate_matches(route, "/other"))

    def test_prefix_with_trailing_slash(self):
        route = Route(host="x.example", matches=(
            MatchEntry(paths=(PathMatch(type="prefix", value="/api/"),)),
        ))
        self.assertTrue(evaluate_matches(route, "/api/foo"))
        self.assertFalse(evaluate_matches(route, "/apifoo"))

    def test_exact_match(self):
        route = Route(host="x.example", matches=(
            MatchEntry(paths=(PathMatch(type="exact", value="/health"),)),
        ))
        self.assertTrue(evaluate_matches(route, "/health"))
        self.assertFalse(evaluate_matches(route, "/health/deep"))
        self.assertFalse(evaluate_matches(route, "/other"))

    def test_regex_match(self):
        import re
        route = Route(host="x.example", matches=(
            MatchEntry(paths=(PathMatch(
                type="regex", value=r"^/v[0-9]+/",
                compiled=re.compile(r"^/v[0-9]+/"),
            ),)),
        ))
        self.assertTrue(evaluate_matches(route, "/v1/messages"))
        self.assertTrue(evaluate_matches(route, "/v42/data"))
        self.assertFalse(evaluate_matches(route, "/api/v1/"))

    def test_method_filter(self):
        route = Route(host="x.example", matches=(
            MatchEntry(methods=("GET", "HEAD")),
        ))
        self.assertTrue(evaluate_matches(route, "/any", "GET"))
        self.assertTrue(evaluate_matches(route, "/any", "HEAD"))
        self.assertFalse(evaluate_matches(route, "/any", "POST"))

    def test_header_exact_match(self):
        route = Route(host="x.example", matches=(
            MatchEntry(headers=(
                HeaderMatch(name="Content-Type", value="application/json"),
            )),
        ))
        self.assertTrue(evaluate_matches(
            route, "/any", "GET",
            {"content-type": "application/json"},
        ))
        self.assertFalse(evaluate_matches(
            route, "/any", "GET",
            {"content-type": "text/html"},
        ))
        self.assertFalse(evaluate_matches(route, "/any", "GET", {}))

    def test_header_regex_match(self):
        import re
        route = Route(host="x.example", matches=(
            MatchEntry(headers=(
                HeaderMatch(
                    name="Accept", value=r"application/.*",
                    type="regex", compiled=re.compile(r"application/.*"),
                ),
            )),
        ))
        self.assertTrue(evaluate_matches(
            route, "/any", "GET", {"accept": "application/json"},
        ))
        self.assertFalse(evaluate_matches(
            route, "/any", "GET", {"accept": "text/html"},
        ))

    def test_and_within_entry(self):
        route = Route(host="x.example", matches=(
            MatchEntry(
                paths=(PathMatch(type="prefix", value="/api"),),
                methods=("POST",),
            ),
        ))
        self.assertTrue(evaluate_matches(route, "/api/data", "POST"))
        self.assertFalse(evaluate_matches(route, "/api/data", "GET"))
        self.assertFalse(evaluate_matches(route, "/other", "POST"))

    def test_or_across_entries(self):
        route = Route(host="x.example", matches=(
            MatchEntry(
                paths=(PathMatch(type="prefix", value="/read"),),
                methods=("GET",),
            ),
            MatchEntry(
                paths=(PathMatch(type="exact", value="/write"),),
                methods=("POST",),
            ),
        ))
        self.assertTrue(evaluate_matches(route, "/read/foo", "GET"))
        self.assertTrue(evaluate_matches(route, "/write", "POST"))
        self.assertFalse(evaluate_matches(route, "/read/foo", "POST"))
        self.assertFalse(evaluate_matches(route, "/write", "GET"))

    def test_multiple_paths_or_within_entry(self):
        route = Route(host="x.example", matches=(
            MatchEntry(paths=(
                PathMatch(type="prefix", value="/a"),
                PathMatch(type="prefix", value="/b"),
            )),
        ))
        self.assertTrue(evaluate_matches(route, "/a/foo"))
        self.assertTrue(evaluate_matches(route, "/b/bar"))
        self.assertFalse(evaluate_matches(route, "/c/baz"))


# --- match_route ---------------------------------------------------------


class TestMatchRoute(unittest.TestCase):
    ROUTES = (
        Route(host="api.github.com"),
        Route(host="github.com", matches=(
            MatchEntry(paths=(PathMatch(type="prefix", value="/x/"),)),
        )),
    )

    def test_exact_match(self):
        r = match_route(self.ROUTES, "api.github.com")
        self.assertIsNotNone(r)
        self.assertEqual("api.github.com", r.host)  # type: ignore

    def test_case_insensitive(self):
        r = match_route(self.ROUTES, "API.GitHub.COM")
        self.assertIsNotNone(r)
        self.assertEqual("api.github.com", r.host)  # type: ignore

    def test_no_match_returns_none(self):
        self.assertIsNone(match_route(self.ROUTES, "elsewhere.example"))

    def test_no_substring_or_prefix_matching(self):
        self.assertIsNone(match_route(self.ROUTES, "evil.api.github.com"))

    def test_wildcard_hosts_not_supported(self):
        routes = (Route(host="*.example.com"),)
        self.assertIsNone(match_route(routes, "foo.example.com"))
        self.assertIsNone(match_route(routes, "example.com"))


# --- decide --------------------------------------------------------------


class TestDecide(unittest.TestCase):
    def test_no_matching_route_blocks(self):
        d = decide((), "elsewhere.example", "/anything", {})
        self.assertEqual("block", d.action)
        self.assertIn("allowlist", d.reason)
        self.assertIn("'elsewhere.example'", d.reason)

    def test_matches_prefix_forwards(self):
        d = decide(
            (Route(host="github.com", matches=(
                MatchEntry(paths=(PathMatch(type="prefix", value="/didericis/"),)),
            )),),
            "github.com", "/didericis/repo", {},
        )
        self.assertEqual("forward", d.action)

    def test_matches_miss_blocks(self):
        d = decide(
            (Route(host="github.com", matches=(
                MatchEntry(paths=(PathMatch(type="prefix", value="/didericis/"),)),
            )),),
            "github.com", "/somebody-else/secret", {},
        )
        self.assertEqual("block", d.action)
        self.assertIn("matches", d.reason)
        self.assertIn("'github.com'", d.reason)

    def test_empty_matches_means_no_constraint(self):
        d = decide(
            (Route(host="api.anthropic.com"),),
            "api.anthropic.com", "/v1/messages", {},
        )
        self.assertEqual("forward", d.action)

    def test_auth_injection_uses_environ_value(self):
        d = decide(
            (Route(host="api.github.com", auth_scheme="Bearer",
                   token_env="EGRESS_TOKEN_0"),),
            "api.github.com", "/repos/x", {"EGRESS_TOKEN_0": "the-token"},
        )
        self.assertEqual("forward", d.action)
        self.assertEqual("Bearer the-token", d.inject_authorization)

    def test_auth_with_missing_token_env_blocks(self):
        d = decide(
            (Route(host="api.github.com", auth_scheme="Bearer",
                   token_env="EGRESS_TOKEN_0"),),
            "api.github.com", "/repos/x", {},
        )
        self.assertEqual("block", d.action)
        self.assertIn("EGRESS_TOKEN_0", d.reason)

    def test_auth_with_empty_token_env_blocks(self):
        d = decide(
            (Route(host="api.github.com", auth_scheme="Bearer",
                   token_env="EGRESS_TOKEN_0"),),
            "api.github.com", "/repos/x", {"EGRESS_TOKEN_0": ""},
        )
        self.assertEqual("block", d.action)

    def test_unauthenticated_route_skips_injection(self):
        d = decide(
            (Route(host="github.com", matches=(
                MatchEntry(paths=(PathMatch(type="prefix", value="/x/"),)),
            )),),
            "github.com", "/x/repo", {"GH_PAT": "should-not-appear"},
        )
        self.assertEqual("forward", d.action)
        self.assertIsNone(d.inject_authorization)

    def test_token_token_scheme(self):
        d = decide(
            (Route(host="git.example", auth_scheme="token",
                   token_env="EGRESS_TOKEN_0"),),
            "git.example", "/api/v1/repos", {"EGRESS_TOKEN_0": "abc"},
        )
        self.assertEqual("token abc", d.inject_authorization)

    def test_method_matching(self):
        route = Route(host="x.example", matches=(
            MatchEntry(methods=("GET",)),
        ))
        d = decide((route,), "x.example", "/any", {},
                    request_method="GET")
        self.assertEqual("forward", d.action)
        d = decide((route,), "x.example", "/any", {},
                    request_method="POST")
        self.assertEqual("block", d.action)

    def test_header_matching(self):
        route = Route(host="x.example", matches=(
            MatchEntry(headers=(
                HeaderMatch(name="Content-Type", value="application/json"),
            )),
        ))
        d = decide((route,), "x.example", "/any", {},
                    request_headers={"content-type": "application/json"})
        self.assertEqual("forward", d.action)
        d = decide((route,), "x.example", "/any", {},
                    request_headers={"content-type": "text/html"})
        self.assertEqual("block", d.action)


# --- Decision dataclass --------------------------------------------------


class TestDecisionDefaults(unittest.TestCase):
    def test_forward_default_has_no_reason_or_inject(self):
        d = Decision(action="forward")
        self.assertEqual("", d.reason)
        self.assertIsNone(d.inject_authorization)


# --- is_git_push_request ------------------------------------------------


class TestIsGitPushRequest(unittest.TestCase):
    def test_post_git_receive_pack_endpoint(self):
        self.assertTrue(is_git_push_request("/owner/repo.git/git-receive-pack", ""))

    def test_info_refs_with_receive_pack_service(self):
        self.assertTrue(is_git_push_request(
            "/owner/repo.git/info/refs",
            "service=git-receive-pack",
        ))

    def test_info_refs_with_extra_query_params(self):
        self.assertTrue(is_git_push_request(
            "/owner/repo.git/info/refs",
            "foo=bar&service=git-receive-pack&z=1",
        ))
        self.assertTrue(is_git_push_request(
            "/owner/repo.git/info/refs",
            "service=git-receive-pack&foo=bar",
        ))

    def test_fetch_endpoints_not_blocked(self):
        self.assertFalse(is_git_push_request(
            "/owner/repo.git/info/refs",
            "service=git-upload-pack",
        ))
        self.assertFalse(is_git_push_request(
            "/owner/repo.git/git-upload-pack", "",
        ))

    def test_info_refs_without_service_not_blocked(self):
        self.assertFalse(is_git_push_request("/x/info/refs", ""))

    def test_unrelated_paths_not_blocked(self):
        self.assertFalse(is_git_push_request("/repos/owner/repo", ""))
        self.assertFalse(is_git_push_request("/v1/messages", ""))
        self.assertFalse(is_git_push_request("/", ""))


class TestGitPushBlockFailFast(unittest.TestCase):
    def test_real_git_push_fails_fast_when_egress_blocks_receive_pack(self):
        seen_paths: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def _handle(self):
                parsed = urlsplit(self.path)
                seen_paths.append(self.path)
                if is_git_push_request(parsed.path, parsed.query):
                    body = (
                        b"egress: git push over HTTPS is not supported; "
                        b"use the bottle.git SSH path (gitleaks-scanned by "
                        b"git-gate's pre-receive hook)."
                    )
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _fmt, *_args):  # type: ignore
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "test"],
                           cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                           cwd=repo, check=True)
            (repo / "README.md").write_text("test\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "test"],
                           cwd=repo, check=True, capture_output=True, text=True)
            remote = f"http://127.0.0.1:{server.server_port}/owner/repo.git"
            subprocess.run(["git", "remote", "add", "origin", remote],
                           cwd=repo, check=True)

            started = time.monotonic()
            result = subprocess.run(
                ["git", "push", "origin", "HEAD:refs/heads/main"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            elapsed = time.monotonic() - started

        self.assertNotEqual(0, result.returncode)
        self.assertLess(elapsed, 5)
        self.assertTrue(
            any("service=git-receive-pack" in p for p in seen_paths),
            f"git did not request receive-pack capabilities; saw {seen_paths!r}",
        )
        self.assertIn("403", result.stderr)


if __name__ == "__main__":
    unittest.main()
