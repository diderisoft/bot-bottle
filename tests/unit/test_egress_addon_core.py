"""Unit: pure-logic core of the egress mitmproxy addon (PRD 0017).

These tests target `egress_addon_core` — the host-importable
half of the addon. The mitmproxy hook wrapper in
`egress_addon.py` is container-only and is not exercised here."""

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
    Route,
    decide,
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
        self.assertEqual((), routes[0].path_allowlist)
        self.assertEqual("", routes[0].auth_scheme)
        self.assertEqual("", routes[0].token_env)

    def test_full_route(self):
        routes = parse_routes({"routes": [{
            "host": "api.github.com",
            "path_allowlist": ["/repos/x/", "/users/x"],
            "auth_scheme": "Bearer",
            "token_env": "EGRESS_TOKEN_0",
        }]})
        r = routes[0]
        self.assertEqual(("/repos/x/", "/users/x"), r.path_allowlist)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", r.token_env)

    def test_order_preserved(self):
        # Host match is exact (not longest-prefix), but the file order
        # is preserved anyway so the operator's mental model matches
        # what the proxy sees.
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
        # auth_scheme without token_env is a renderer bug (the manifest's
        # `auth: { scheme, token_ref }` block writes both at once).
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

    def test_path_allowlist_must_be_absolute(self):
        with self.assertRaises(ValueError) as cm:
            parse_routes({"routes": [{
                "host": "x.example",
                "path_allowlist": ["no-leading-slash/"],
            }]})
        self.assertIn("absolute path prefix", str(cm.exception))

    def test_path_allowlist_items_must_be_strings(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{
                "host": "x.example",
                "path_allowlist": [42],
            }]})

    def test_top_level_must_be_object(self):
        with self.assertRaises(ValueError):
            parse_routes(["not", "an", "object"])

    def test_routes_must_be_list(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": "not a list"})

    def test_route_must_have_host(self):
        with self.assertRaises(ValueError):
            parse_routes({"routes": [{}]})


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
            '    path_allowlist:\n'
            '      - "/v1/"\n'
            '      - "/messages"\n'
        )
        self.assertEqual(1, len(routes))
        r = routes[0]
        self.assertEqual("api.example", r.host)
        self.assertEqual("Bearer", r.auth_scheme)
        self.assertEqual("EGRESS_TOKEN_0", r.token_env)
        self.assertEqual(("/v1/", "/messages"), r.path_allowlist)

    def test_empty_routes_list(self):
        routes = load_routes("routes: []\n")
        self.assertEqual((), routes)

    def test_invalid_yaml_raises_value_error(self):
        # Tab indent is a YamlSubsetError; ValueError is its base.
        with self.assertRaises(ValueError):
            load_routes("routes:\n\t- host: x\n")


# --- match_route ---------------------------------------------------------


class TestMatchRoute(unittest.TestCase):
    ROUTES = (
        Route(host="api.github.com"),
        Route(host="github.com", path_allowlist=("/x/",)),
    )

    def test_exact_match(self):
        r = match_route(self.ROUTES, "api.github.com")
        self.assertIsNotNone(r)
        self.assertEqual("api.github.com", r.host)  # type: ignore

    def test_case_insensitive(self):
        # DNS hostnames are case-insensitive per RFC 1035; mitmproxy
        # surfaces the host as the agent wrote it, which may include
        # uppercase. Lookup must normalise.
        r = match_route(self.ROUTES, "API.GitHub.COM")
        self.assertIsNotNone(r)
        self.assertEqual("api.github.com", r.host)  # type: ignore

    def test_no_match_returns_none(self):
        self.assertIsNone(match_route(self.ROUTES, "elsewhere.example"))

    def test_no_substring_or_prefix_matching(self):
        # api.github.com is in the table; github.com is too. Some
        # other-host shouldn't be matched via a "ends with" check.
        self.assertIsNone(match_route(self.ROUTES, "evil.api.github.com"))

    def test_wildcard_hosts_not_supported(self):
        # `*.example.com` is treated as a literal host string by
        # the exact-only matcher. Removed from the design after
        # the apex/RFC-6125/pipelock-mirror edge cases stacked up.
        routes = (Route(host="*.example.com"),)
        self.assertIsNone(match_route(routes, "foo.example.com"))
        self.assertIsNone(match_route(routes, "example.com"))


# --- decide --------------------------------------------------------------


class TestDecide(unittest.TestCase):
    def test_no_matching_route_blocks(self):
        # Defense-in-depth: egress gates the bottle's allowlist
        # too, not just pipelock. Any host the operator didn't declare
        # in egress.routes is 403'd at egress before it
        # ever reaches pipelock.
        d = decide((), "elsewhere.example", "/anything", {})
        self.assertEqual("block", d.action)
        self.assertIn("allowlist", d.reason)
        self.assertIn("'elsewhere.example'", d.reason)

    def test_path_allowlist_match_forwards(self):
        d = decide(
            (Route(host="github.com", path_allowlist=("/didericis/",)),),
            "github.com", "/didericis/repo", {},
        )
        self.assertEqual("forward", d.action)

    def test_path_allowlist_miss_blocks(self):
        d = decide(
            (Route(host="github.com", path_allowlist=("/didericis/",)),),
            "github.com", "/somebody-else/secret", {},
        )
        self.assertEqual("block", d.action)
        self.assertIn("path_allowlist", d.reason)
        self.assertIn("'github.com'", d.reason)

    def test_empty_path_allowlist_means_no_constraint(self):
        # Bare-pass route: declared but no path filtering.
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
        # The route declared auth but the secret isn't in the
        # container's env — operator misconfig at start-time, blocked
        # with a clear reason rather than forwarding an unauthenticated
        # request the upstream would reject.
        d = decide(
            (Route(host="api.github.com", auth_scheme="Bearer",
                   token_env="EGRESS_TOKEN_0"),),
            "api.github.com", "/repos/x", {},
        )
        self.assertEqual("block", d.action)
        self.assertIn("EGRESS_TOKEN_0", d.reason)

    def test_auth_with_empty_token_env_blocks(self):
        # Empty env var is treated the same as unset — we don't inject
        # a literal "Bearer " (blank token) which would burn the
        # upstream rate limit with a 401.
        d = decide(
            (Route(host="api.github.com", auth_scheme="Bearer",
                   token_env="EGRESS_TOKEN_0"),),
            "api.github.com", "/repos/x", {"EGRESS_TOKEN_0": ""},
        )
        self.assertEqual("block", d.action)

    def test_unauthenticated_route_skips_injection(self):
        d = decide(
            (Route(host="github.com", path_allowlist=("/x/",)),),
            "github.com", "/x/repo", {"GH_PAT": "should-not-appear"},
        )
        self.assertEqual("forward", d.action)
        self.assertIsNone(d.inject_authorization)

    def test_token_token_scheme(self):
        # Gitea uses `Authorization: token <pat>` (sidesteps
        # go-gitea/gitea#16734). The addon is scheme-agnostic.
        d = decide(
            (Route(host="git.example", auth_scheme="token",
                   token_env="EGRESS_TOKEN_0"),),
            "git.example", "/api/v1/repos", {"EGRESS_TOKEN_0": "abc"},
        )
        self.assertEqual("token abc", d.inject_authorization)


# --- Decision dataclass --------------------------------------------------


class TestDecisionDefaults(unittest.TestCase):
    def test_forward_default_has_no_reason_or_inject(self):
        d = Decision(action="forward")
        self.assertEqual("", d.reason)
        self.assertIsNone(d.inject_authorization)


# --- is_git_push_request ------------------------------------------------


class TestIsGitPushRequest(unittest.TestCase):
    def test_post_git_receive_pack_endpoint(self):
        # The POST that carries the actual push payload.
        self.assertTrue(is_git_push_request("/owner/repo.git/git-receive-pack", ""))

    def test_info_refs_with_receive_pack_service(self):
        # The capability advertisement GET that precedes a push.
        self.assertTrue(is_git_push_request(
            "/owner/repo.git/info/refs",
            "service=git-receive-pack",
        ))

    def test_info_refs_with_extra_query_params(self):
        # service= may appear with other params in any order.
        self.assertTrue(is_git_push_request(
            "/owner/repo.git/info/refs",
            "foo=bar&service=git-receive-pack&z=1",
        ))
        self.assertTrue(is_git_push_request(
            "/owner/repo.git/info/refs",
            "service=git-receive-pack&foo=bar",
        ))

    def test_fetch_endpoints_not_blocked(self):
        # `service=git-upload-pack` is fetch; never blocked.
        self.assertFalse(is_git_push_request(
            "/owner/repo.git/info/refs",
            "service=git-upload-pack",
        ))
        self.assertFalse(is_git_push_request(
            "/owner/repo.git/git-upload-pack", "",
        ))

    def test_info_refs_without_service_not_blocked(self):
        # Bare info/refs (no query) defaults to git-upload-pack on
        # the server side; not push.
        self.assertFalse(is_git_push_request("/x/info/refs", ""))

    def test_unrelated_paths_not_blocked(self):
        self.assertFalse(is_git_push_request("/repos/owner/repo", ""))
        self.assertFalse(is_git_push_request("/v1/messages", ""))
        self.assertFalse(is_git_push_request("/", ""))


class TestGitPushBlockFailFast(unittest.TestCase):
    def test_real_git_push_fails_fast_when_egress_blocks_receive_pack(self):
        """A real git client should see egress's HTTPS-push 403 and exit.

        The local server stands in for the egress proxy response after
        CONNECT/TLS interception; git smart-HTTP uses the same paths over
        plain HTTP here, which keeps this regression test hermetic.
        """

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
