"""Unit tests for framework-neutral supervisor MCP dispatch."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from bot_bottle.gateway.supervisor.mcp_dispatch import (
    Handlers,
    MethodNotFoundError,
    dispatch,
)
from bot_bottle.contract import supervise as _sv


@dataclass(frozen=True)
class _Request:
    method: str
    params: dict[str, object]


class TestDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []

        def handler(name: str):
            def call(_params: dict[str, object]) -> str:
                self.calls.append(name)
                return name

            return call

        self.handlers = Handlers(
            initialize=handler("initialize"),
            tools_list=handler("tools_list"),
            list_routes=handler("list_routes"),
            check_proposal=handler("check_proposal"),
            propose=handler("propose"),
        )

    def request(self, method: str, **params: object) -> _Request:
        return _Request(method=method, params=params)

    def test_routes_protocol_methods(self) -> None:
        self.assertEqual(
            "initialize", dispatch(self.request("initialize"), self.handlers),
        )
        self.assertEqual(
            "tools_list", dispatch(self.request("tools/list"), self.handlers),
        )
        self.assertIsNone(
            dispatch(self.request("notifications/initialized"), self.handlers),
        )

    def test_routes_each_tool_class(self) -> None:
        self.assertEqual(
            "list_routes",
            dispatch(
                self.request("tools/call", name=_sv.TOOL_LIST_EGRESS_ROUTES),
                self.handlers,
            ),
        )
        self.assertEqual(
            "check_proposal",
            dispatch(
                self.request("tools/call", name=_sv.TOOL_CHECK_PROPOSAL),
                self.handlers,
            ),
        )
        self.assertEqual(
            "propose",
            dispatch(self.request("tools/call", name=_sv.TOOL_EGRESS_ALLOW), self.handlers),
        )

    def test_unknown_method_is_typed(self) -> None:
        with self.assertRaisesRegex(MethodNotFoundError, "unknown"):
            dispatch(self.request("unknown"), self.handlers)


if __name__ == "__main__":
    unittest.main()
