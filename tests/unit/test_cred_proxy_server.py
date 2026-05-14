"""Unit: cred-proxy server pure functions — route parsing, route
selection, header injection (PRD 0010)."""

import unittest

from claude_bottle.cred_proxy_server import (
    Route,
    build_forward_headers,
    filter_response_headers,
    is_git_push_request,
    load_tokens,
    parse_routes,
    select_route,
)


class TestParseRoutes(unittest.TestCase):
    def test_parses_minimal_payload(self):
        routes = parse_routes({"routes": [
            {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer", "token_env": "CRED_PROXY_TOKEN_0"},
        ]})
        self.assertEqual(1, len(routes))
        r = routes[0]
        self.assertEqual("/anthropic/", r.path)
        self.assertEqual("https", r.upstream_scheme)
        self.assertEqual("api.anthropic.com", r.upstream_host)
        self.assertEqual(443, r.upstream_port)
        self.assertEqual("", r.upstream_base_path)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("CRED_PROXY_TOKEN_0", r.token_env)

    def test_extracts_port_from_upstream(self):
        routes = parse_routes({"routes": [
            {"path": "/gitea/gitea.dideric.is/",
             "upstream": "https://gitea.dideric.is:30443",
             "auth_scheme": "token", "token_env": "CRED_PROXY_TOKEN_0"},
        ]})
        self.assertEqual(30443, routes[0].upstream_port)

    def test_sorted_by_descending_path_length(self):
        # /a/b/ should come before /a/ so longest-prefix is first.
        routes = parse_routes({"routes": [
            {"path": "/a/", "upstream": "https://x.example",
             "auth_scheme": "Bearer", "token_env": "T1"},
            {"path": "/a/b/", "upstream": "https://y.example",
             "auth_scheme": "Bearer", "token_env": "T2"},
        ]})
        self.assertEqual("/a/b/", routes[0].path)
        self.assertEqual("/a/", routes[1].path)

    def test_bad_path_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [
                {"path": "no-leading-slash", "upstream": "https://x",
                 "auth_scheme": "Bearer", "token_env": "T"},
            ]})

    def test_non_http_scheme_rejected(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [
                {"path": "/x/", "upstream": "ftp://x.example/",
                 "auth_scheme": "Bearer", "token_env": "T"},
            ]})


class TestSelectRoute(unittest.TestCase):
    def setUp(self):
        self.routes = parse_routes({"routes": [
            {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer", "token_env": "T_A"},
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_env": "T_G"},
            {"path": "/gitea/gitea.dideric.is/",
             "upstream": "https://gitea.dideric.is",
             "auth_scheme": "token", "token_env": "T_T"},
        ]})

    def test_matches_prefix(self):
        r = select_route(self.routes, "/anthropic/v1/messages")
        assert r is not None
        self.assertEqual("/anthropic/", r.path)

    def test_no_match_returns_none(self):
        self.assertIsNone(select_route(self.routes, "/other/path"))

    def test_picks_longest_prefix(self):
        routes = parse_routes({"routes": [
            {"path": "/a/", "upstream": "https://x.example",
             "auth_scheme": "Bearer", "token_env": "T1"},
            {"path": "/a/long/", "upstream": "https://y.example",
             "auth_scheme": "Bearer", "token_env": "T2"},
        ]})
        r = select_route(routes, "/a/long/sub")
        assert r is not None
        self.assertEqual("/a/long/", r.path)


class TestBuildForwardHeaders(unittest.TestCase):
    def test_strips_authorization_and_injects(self):
        headers = build_forward_headers(
            [("Authorization", "Bearer stolen-token"),
             ("Content-Type", "application/json")],
            auth_scheme="Bearer",
            token="real-token",
            upstream_host="api.anthropic.com",
        )
        names = [n.lower() for n, _ in headers]
        # Only one Authorization remains, with the injected value.
        auth_values = [v for n, v in headers if n.lower() == "authorization"]
        self.assertEqual(["Bearer real-token"], auth_values)
        self.assertEqual(1, names.count("authorization"))
        # Content-Type passes through.
        self.assertIn(("Content-Type", "application/json"), headers)

    def test_strips_authorization_case_insensitive(self):
        headers = build_forward_headers(
            [("authorization", "Bearer stolen")],
            auth_scheme="Bearer",
            token="real",
            upstream_host="x.example",
        )
        auth_values = [v for n, v in headers if n.lower() == "authorization"]
        self.assertEqual(["Bearer real"], auth_values)

    def test_strips_hop_by_hop(self):
        headers = build_forward_headers(
            [("Connection", "keep-alive, x-custom"),
             ("X-Custom", "should-be-dropped"),
             ("Keep-Alive", "300"),
             ("Transfer-Encoding", "chunked"),
             ("X-Real", "kept")],
            auth_scheme="Bearer",
            token="t",
            upstream_host="x.example",
        )
        names = [n.lower() for n, _ in headers]
        self.assertNotIn("connection", names)
        self.assertNotIn("keep-alive", names)
        self.assertNotIn("transfer-encoding", names)
        self.assertNotIn("x-custom", names)  # listed in Connection: -> hop-by-hop
        self.assertIn("x-real", names)

    def test_strips_content_length(self):
        # http.client recomputes Content-Length; passing it through
        # double-counts and breaks the upstream.
        headers = build_forward_headers(
            [("Content-Length", "999")],
            auth_scheme="Bearer", token="t", upstream_host="x.example",
        )
        names = [n.lower() for n, _ in headers]
        self.assertNotIn("content-length", names)

    def test_sets_host_to_upstream(self):
        headers = build_forward_headers(
            [("Host", "cred-proxy:9099")],
            auth_scheme="Bearer", token="t", upstream_host="api.anthropic.com",
        )
        host_values = [v for n, v in headers if n.lower() == "host"]
        self.assertEqual(["api.anthropic.com"], host_values)

    def test_uses_token_scheme(self):
        # gitea uses Authorization: token <pat>, not Bearer.
        headers = build_forward_headers(
            [],
            auth_scheme="token", token="abc123", upstream_host="gitea.dideric.is",
        )
        auth_values = [v for n, v in headers if n.lower() == "authorization"]
        self.assertEqual(["token abc123"], auth_values)


class TestFilterResponseHeaders(unittest.TestCase):
    def test_strips_hop_by_hop_only(self):
        out = filter_response_headers([
            ("Content-Type", "text/event-stream"),
            ("Connection", "close"),
            ("Transfer-Encoding", "chunked"),
            ("Cache-Control", "no-cache"),
        ])
        names = [n.lower() for n, _ in out]
        self.assertIn("content-type", names)
        self.assertIn("cache-control", names)
        self.assertNotIn("connection", names)
        self.assertNotIn("transfer-encoding", names)


class TestIsGitPushRequest(unittest.TestCase):
    """git push over HTTPS goes through /info/refs?service=git-receive-pack
    (capabilities probe) then POST /git-receive-pack (the push body).
    Fetches use /git-upload-pack and are not blocked — the bypass we're
    closing is push, since git-gate's gitleaks pre-receive is the scanner
    for outbound git data."""

    def test_push_capabilities_probe_blocked(self):
        self.assertTrue(is_git_push_request(
            "/gh-git/owner/repo.git/info/refs",
            "service=git-receive-pack",
        ))

    def test_push_body_blocked(self):
        self.assertTrue(is_git_push_request(
            "/gh-git/owner/repo.git/git-receive-pack", "",
        ))

    def test_fetch_capabilities_allowed(self):
        self.assertFalse(is_git_push_request(
            "/gh-git/owner/repo.git/info/refs",
            "service=git-upload-pack",
        ))

    def test_fetch_body_allowed(self):
        self.assertFalse(is_git_push_request(
            "/gh-git/owner/repo.git/git-upload-pack", "",
        ))

    def test_rest_api_allowed(self):
        # tea/gh-style REST calls hit /api/v1/... — unrelated.
        self.assertFalse(is_git_push_request(
            "/gitea/gitea.dideric.is/api/v1/repos/x/y", "",
        ))

    def test_push_with_extra_query_params(self):
        # `service` may appear with other params in any order.
        self.assertTrue(is_git_push_request(
            "/gh-git/owner/repo.git/info/refs",
            "trace=1&service=git-receive-pack",
        ))


class TestLoadTokens(unittest.TestCase):
    def test_reads_per_route_env(self):
        routes = (
            Route("/a/", "https", "x", 443, "", "Bearer", "T_0"),
            Route("/b/", "https", "y", 443, "", "Bearer", "T_1"),
        )
        out = load_tokens(routes, {"T_0": "val0", "T_1": "val1"})
        self.assertEqual({"T_0": "val0", "T_1": "val1"}, out)

    def test_missing_env_yields_empty_string(self):
        # The handler returns 500 at request time rather than the
        # server refusing to start. This keeps the operator's failure
        # signal in the cred-proxy's logs.
        routes = (Route("/a/", "https", "x", 443, "", "Bearer", "T_0"),)
        out = load_tokens(routes, {})
        self.assertEqual({"T_0": ""}, out)


if __name__ == "__main__":
    unittest.main()
