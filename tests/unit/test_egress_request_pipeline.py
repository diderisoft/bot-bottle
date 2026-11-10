"""Unit tests for framework-neutral egress request policy stages."""

from __future__ import annotations

import unittest

from bot_bottle.gateway.egress.types import ANY_REQUEST

from bot_bottle.gateway.egress.request_pipeline import (
    GIT_PUSH_BLOCK_REASON,
    evaluate_route_policy,
    git_block_reason,
    strip_credential_query,
)
from bot_bottle.gateway.egress.types import (
    Config, HeaderMatch, LOG_FULL, MatchEntry, Route,
)


class TestStripCredentialQuery(unittest.TestCase):
    def test_no_query_is_unchanged(self) -> None:
        self.assertEqual("/a/b", strip_credential_query("/a/b"))

    def test_clean_query_is_kept_byte_for_byte(self) -> None:
        path = "/s?q=a%20b&tokenizer=x&key_id=1&&flag"
        self.assertEqual(path, strip_credential_query(path))

    def test_credential_params_are_dropped(self) -> None:
        self.assertEqual(
            "/s?q=1&page=2",
            strip_credential_query(
                "/s?access_token=a&q=1&KEY=b&page=2&api%5Fkey=c&apikey&private_token=d"),
        )

    def test_only_credentials_drops_the_question_mark(self) -> None:
        self.assertEqual("/s", strip_credential_query("/s?token=x"))


class TestGitPolicy(unittest.TestCase):
    def test_push_is_always_blocked(self) -> None:
        reason = git_block_reason(
            (), "git.example.com", "/repo.git/git-receive-pack", "",
        )
        self.assertEqual(GIT_PUSH_BLOCK_REASON, reason)

    def test_fetch_requires_route_opt_in(self) -> None:
        path = "/repo.git/git-upload-pack"
        blocked = git_block_reason((), "git.example.com", path, "")
        allowed = git_block_reason(
            (Route(host="git.example.com", git_fetch=True),),
            "git.example.com",
            path,
            "",
        )
        self.assertTrue(blocked)
        self.assertEqual("", allowed)

    def test_non_git_request_is_not_decided_here(self) -> None:
        self.assertEqual(
            "",
            git_block_reason((), "api.example.com", "/v1/messages", ""),
        )


class TestRoutePolicy(unittest.TestCase):
    def test_strips_agent_auth_and_injects_gateway_auth(self) -> None:
        route = Route(
            host="api.example.com",
            auth_scheme="Bearer",
            token_env="API_TOKEN",
            matches=ANY_REQUEST,
        )
        result = evaluate_route_policy(
            Config(routes=(route,)),
            route,
            host="api.example.com",
            request_path="/v1/messages",
            method="POST",
            headers={"Authorization": "agent-secret"},
            env={"API_TOKEN": "gateway-secret"},
        )
        self.assertTrue(result.strip_authorization)
        self.assertEqual("Bearer gateway-secret", result.inject_authorization)
        self.assertFalse(result.block_reason)

    def test_preserved_auth_participates_in_matching(self) -> None:
        route = Route(host="registry.example.com", preserve_auth=True)
        result = evaluate_route_policy(
            Config(routes=(route,), log=LOG_FULL),
            route,
            host="registry.example.com",
            request_path="/v2/",
            method="GET",
            headers={"Authorization": "Bearer agent-token"},
            env={},
        )
        self.assertFalse(result.strip_authorization)
        self.assertTrue(result.log_request)

    def test_named_header_injects_the_raw_token(self) -> None:
        route = Route(host="api.example.com", auth_header="x-custom-key", token_env="T")
        result = evaluate_route_policy(
            Config(routes=(route,)), route, host="api.example.com",
            request_path="/", method="GET", headers={}, env={"T": "raw-key"})
        self.assertEqual("raw-key", result.inject_authorization)
        self.assertEqual("x-custom-key", result.inject_header)
        self.assertIn("x-custom-key", result.strip_headers)
        self.assertIn("authorization", result.strip_headers)

    def test_matching_sees_headers_without_agent_credentials(self) -> None:
        route = Route(host="api.example.com", matches=(MatchEntry(
            headers=(HeaderMatch(name="x-api-key", value="k"),)),))
        result = evaluate_route_policy(
            Config(routes=(route,)), route, host="api.example.com",
            request_path="/", method="GET", headers={"X-Api-Key": "k"}, env={})
        self.assertEqual("route", result.block_code)

    def test_missing_route_fails_closed(self) -> None:
        result = evaluate_route_policy(
            Config(routes=(), deny_reason="not allowed"),
            None,
            host="blocked.example.com",
            request_path="/",
            method="GET",
            headers={},
            env={},
        )
        self.assertEqual("not allowed", result.block_reason)
        self.assertEqual("config", result.block_code)

    def test_an_unlisted_host_is_an_allowlist_block(self) -> None:
        result = evaluate_route_policy(
            Config(routes=()), None, host="x.example", request_path="/",
            method="GET", headers={}, env={})
        self.assertEqual("allowlist", result.block_code)


if __name__ == "__main__":
    unittest.main()
