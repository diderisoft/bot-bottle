"""Unit tests for framework-neutral egress request policy stages."""

from __future__ import annotations

import unittest

from bot_bottle.gateway.egress.request_pipeline import (
    GIT_PUSH_BLOCK_REASON,
    evaluate_route_policy,
    git_block_reason,
)
from bot_bottle.gateway.egress.types import Config, LOG_FULL, Route


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
