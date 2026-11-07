"""Unit: launch-bottle tool — format validation, access comparison, trimmed
bottle builder, and bottle_state.parent field."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any


def _no_op(*_a: Any, **_kw: Any) -> None:
    return None


# --- parse_launch_proposal ---

class TestParseLaunchProposal(unittest.TestCase):
    def _parse(self, d: dict[str, Any]) -> dict[str, Any]:
        from bot_bottle.control.hostctl.launch_bottle import parse_launch_proposal
        return parse_launch_proposal(json.dumps(d))

    def test_valid_minimal(self):
        result = self._parse({"agent": "my-agent", "prompt": "do the thing",
                              "bottles": [], "label": ""})
        self.assertEqual(result["agent"], "my-agent")
        self.assertEqual(result["prompt"], "do the thing")
        self.assertEqual(result["bottles"], [])
        self.assertEqual(result["label"], "")

    def test_valid_with_bottles_and_label(self):
        result = self._parse({"agent": "coder", "prompt": "write tests",
                              "bottles": ["app-dev", "code-review"], "label": "test runner"})
        self.assertEqual(result["bottles"], ["app-dev", "code-review"])
        self.assertEqual(result["label"], "test runner")

    def test_missing_agent(self):
        from bot_bottle.control.hostctl.launch_bottle import parse_launch_proposal
        with self.assertRaises(ValueError):
            parse_launch_proposal(json.dumps({"prompt": "x", "bottles": []}))

    def test_empty_prompt(self):
        from bot_bottle.control.hostctl.launch_bottle import parse_launch_proposal
        with self.assertRaises(ValueError):
            parse_launch_proposal(json.dumps({"agent": "a", "prompt": "", "bottles": []}))

    def test_invalid_json(self):
        from bot_bottle.control.hostctl.launch_bottle import parse_launch_proposal
        with self.assertRaises(ValueError):
            parse_launch_proposal("{not json}")

    def test_not_object(self):
        from bot_bottle.control.hostctl.launch_bottle import parse_launch_proposal
        with self.assertRaises(ValueError):
            parse_launch_proposal(json.dumps([1, 2, 3]))


# --- compare_access ---

class TestCompareAccess(unittest.TestCase):
    def _elem(self, key: str, kind: str | None = None) -> dict[str, Any]:
        k = kind or key.split(":", 1)[0]
        return {"key": key, "kind": k, "name": key.split(":", 1)[1],
                "detail": "", "source": "test", "builtin": False, "fixed": False, "also": []}

    def test_all_within(self):
        from bot_bottle.control.hostctl.launch_bottle import compare_access
        parent = [self._elem("egress:api.example.com"), self._elem("repo:app"),
                  self._elem("env:TOKEN")]
        child = [self._elem("egress:api.example.com"), self._elem("repo:app")]
        within, extra = compare_access(child, parent)
        self.assertEqual(len(within), 2)
        self.assertEqual(extra, [])

    def test_extra_egress(self):
        from bot_bottle.control.hostctl.launch_bottle import compare_access
        parent = [self._elem("egress:api.example.com")]
        child = [self._elem("egress:api.example.com"), self._elem("egress:extra.example.com")]
        within, extra = compare_access(child, parent)
        self.assertEqual(len(within), 1)
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]["key"], "egress:extra.example.com")

    def test_extra_repo(self):
        from bot_bottle.control.hostctl.launch_bottle import compare_access
        parent = [self._elem("repo:app")]
        child = [self._elem("repo:app"), self._elem("repo:extra-service")]
        _, extra = compare_access(child, parent)
        self.assertEqual([e["key"] for e in extra], ["repo:extra-service"])

    def test_extra_env(self):
        from bot_bottle.control.hostctl.launch_bottle import compare_access
        parent = [self._elem("env:ALLOWED")]
        child = [self._elem("env:ALLOWED"), self._elem("env:SECRET_KEY")]
        _, extra = compare_access(child, parent)
        self.assertEqual([e["key"] for e in extra], ["env:SECRET_KEY"])

    def test_resources_always_within(self):
        from bot_bottle.control.hostctl.launch_bottle import compare_access
        parent: list[dict[str, Any]] = []
        child = [self._elem("resources:memory")]
        within, extra = compare_access(child, parent)
        self.assertEqual(len(within), 1)
        self.assertEqual(extra, [])

    def test_child_subset(self):
        from bot_bottle.control.hostctl.launch_bottle import compare_access
        parent = [self._elem("egress:a.com"), self._elem("egress:b.com"), self._elem("repo:x")]
        child = [self._elem("egress:a.com")]
        within, extra = compare_access(child, parent)
        self.assertEqual(len(within), 1)
        self.assertEqual(extra, [])


# --- gateway server: _build_launch_bottle_proposed ---

class TestBuildLaunchBottleProposed(unittest.TestCase):
    def _build(self, args: dict[str, Any]) -> Any:
        from bot_bottle.gateway.supervisor.server import _build_launch_bottle_proposed
        return _build_launch_bottle_proposed("launch-bottle", args)

    def test_valid(self):
        result = self._build({"agent": "my-agent", "prompt": "do work",
                              "bottles": ["app-dev"], "label": "worker",
                              "justification": "need parallel work"})
        d = json.loads(result)
        self.assertEqual(d["agent"], "my-agent")
        self.assertEqual(d["prompt"], "do work")
        self.assertEqual(d["bottles"], ["app-dev"])
        self.assertEqual(d["label"], "worker")

    def test_missing_agent(self):
        from bot_bottle.gateway.supervisor.server import _RpcClientError, _build_launch_bottle_proposed
        with self.assertRaises(_RpcClientError):
            _build_launch_bottle_proposed("launch-bottle", {"prompt": "x", "justification": "y"})

    def test_empty_prompt(self):
        from bot_bottle.gateway.supervisor.server import _RpcClientError, _build_launch_bottle_proposed
        with self.assertRaises(_RpcClientError):
            _build_launch_bottle_proposed("launch-bottle",
                                          {"agent": "a", "prompt": "", "justification": "y"})

    def test_too_many_bottles(self):
        from bot_bottle.gateway.supervisor.server import _RpcClientError, _build_launch_bottle_proposed
        with self.assertRaises(_RpcClientError):
            _build_launch_bottle_proposed("launch-bottle",
                                          {"agent": "a", "prompt": "x",
                                           "bottles": ["b"] * 9, "justification": "y"})

    def test_prompt_too_long(self):
        from bot_bottle.gateway.supervisor.server import _RpcClientError, _build_launch_bottle_proposed
        with self.assertRaises(_RpcClientError):
            _build_launch_bottle_proposed("launch-bottle",
                                          {"agent": "a", "prompt": "x" * 20_001,
                                           "justification": "y"})

    def test_invalid_bottle_name(self):
        from bot_bottle.gateway.supervisor.server import _RpcClientError, _build_launch_bottle_proposed
        with self.assertRaises(_RpcClientError):
            _build_launch_bottle_proposed("launch-bottle",
                                          {"agent": "a", "prompt": "x",
                                           "bottles": ["bad bottle!"], "justification": "y"})

    def test_label_too_long(self):
        from bot_bottle.gateway.supervisor.server import _RpcClientError, _build_launch_bottle_proposed
        with self.assertRaises(_RpcClientError):
            _build_launch_bottle_proposed("launch-bottle",
                                          {"agent": "a", "prompt": "x",
                                           "label": "l" * 129, "justification": "y"})

    def test_bottles_defaults_empty(self):
        result = self._build({"agent": "a", "prompt": "do it", "justification": "y"})
        d = json.loads(result)
        self.assertEqual(d["bottles"], [])

    def test_label_defaults_empty(self):
        result = self._build({"agent": "a", "prompt": "do it", "justification": "y"})
        d = json.loads(result)
        self.assertEqual(d["label"], "")


# --- TOOLS tuple includes launch-bottle ---

class TestSuperviseContract(unittest.TestCase):
    def test_launch_bottle_in_tools(self):
        from bot_bottle.contract.supervise import TOOL_LAUNCH_BOTTLE, TOOLS
        self.assertIn(TOOL_LAUNCH_BOTTLE, TOOLS)
        self.assertEqual(TOOL_LAUNCH_BOTTLE, "launch-bottle")


# --- bottle_state.parent field ---

class TestBottleMetadataParent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._prev_root = os.environ.get("BOT_BOTTLE_ROOT")
        os.environ["BOT_BOTTLE_ROOT"] = self._tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._prev_root is None:
            os.environ.pop("BOT_BOTTLE_ROOT", None)
        else:
            os.environ["BOT_BOTTLE_ROOT"] = self._prev_root

    def test_default_parent_is_empty(self):
        from bot_bottle.bottle_state import BottleMetadata
        m = BottleMetadata(identity="abc", agent_name="agent", cwd="",
                           copy_cwd=False, started_at="2026-01-01T00:00:00Z")
        self.assertEqual(m.parent, "")

    def test_write_and_read_parent(self):
        from bot_bottle.bottle_state import BottleMetadata, read_metadata, write_metadata
        m = BottleMetadata(identity="child-id", agent_name="bot", cwd="",
                           copy_cwd=False, started_at="2026-01-01T00:00:00Z",
                           parent="parent-id")
        write_metadata(m)
        got = read_metadata("child-id")
        self.assertIsNotNone(got)
        self.assertEqual(got.parent, "parent-id")  # type: ignore[union-attr]

    def test_read_metadata_missing_parent_defaults_empty(self):
        import json
        from bot_bottle.bottle_state import BottleMetadata, read_metadata, write_metadata
        m = BottleMetadata(identity="old-id", agent_name="bot", cwd="",
                           copy_cwd=False, started_at="2026-01-01T00:00:00Z")
        path = write_metadata(m)
        # Simulate old metadata without parent field.
        raw = json.loads(path.read_text())
        raw.pop("parent", None)
        path.write_text(json.dumps(raw))
        got = read_metadata("old-id")
        self.assertIsNotNone(got)
        self.assertEqual(got.parent, "")  # type: ignore[union-attr]

    def test_record_parent(self):
        from bot_bottle.bottle_state import BottleMetadata, read_metadata, record_parent, write_metadata
        m = BottleMetadata(identity="cid", agent_name="bot", cwd="",
                           copy_cwd=False, started_at="2026-01-01T00:00:00Z")
        write_metadata(m)
        record_parent("cid", "pid")
        got = read_metadata("cid")
        self.assertIsNotNone(got)
        self.assertEqual(got.parent, "pid")  # type: ignore[union-attr]

    def test_record_parent_noop_without_metadata(self):
        from bot_bottle.bottle_state import record_parent
        # Should not raise even if no metadata.
        record_parent("nonexistent-id", "parent-id")


# --- children module ---

class TestChildren(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._prev_root = os.environ.get("BOT_BOTTLE_ROOT")
        os.environ["BOT_BOTTLE_ROOT"] = self._tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._prev_root is None:
            os.environ.pop("BOT_BOTTLE_ROOT", None)
        else:
            os.environ["BOT_BOTTLE_ROOT"] = self._prev_root

    def test_is_launch_bottle_child_false_without_metadata(self):
        from bot_bottle.control.hostctl.children import is_launch_bottle_child
        self.assertFalse(is_launch_bottle_child("no-such-bottle"))

    def test_is_launch_bottle_child_false_no_parent(self):
        from bot_bottle.bottle_state import BottleMetadata, write_metadata
        from bot_bottle.control.hostctl.children import is_launch_bottle_child
        write_metadata(BottleMetadata(identity="bot-a", agent_name="a", cwd="",
                                      copy_cwd=False, started_at="now"))
        self.assertFalse(is_launch_bottle_child("bot-a"))

    def test_is_launch_bottle_child_true_with_parent(self):
        from bot_bottle.bottle_state import BottleMetadata, write_metadata
        from bot_bottle.control.hostctl.children import is_launch_bottle_child
        write_metadata(BottleMetadata(identity="child-x", agent_name="a", cwd="",
                                      copy_cwd=False, started_at="now", parent="parent-y"))
        self.assertTrue(is_launch_bottle_child("child-x"))

    def test_running_children_of_counts_running(self):
        from bot_bottle.bottle_state import BottleMetadata, write_metadata
        from bot_bottle.control.hostctl.children import running_children_of
        write_metadata(BottleMetadata(identity="c1", agent_name="a", cwd="",
                                      copy_cwd=False, started_at="now", parent="parent-p"))
        write_metadata(BottleMetadata(identity="c2", agent_name="a", cwd="",
                                      copy_cwd=False, started_at="now", parent="parent-p"))
        write_metadata(BottleMetadata(identity="c3", agent_name="a", cwd="",
                                      copy_cwd=False, started_at="now", parent="other-parent"))

        class FakeBottles:
            def session_state(self, identity: str) -> dict[str, bool]:
                return {"running": identity in ("c1", "c2")}

        count = running_children_of("parent-p", FakeBottles())
        self.assertEqual(count, 2)

    def test_running_children_of_zero_when_stopped(self):
        from bot_bottle.bottle_state import BottleMetadata, write_metadata
        from bot_bottle.control.hostctl.children import running_children_of

        write_metadata(BottleMetadata(identity="c-stopped", agent_name="a", cwd="",
                                      copy_cwd=False, started_at="now", parent="p-id"))

        class FakeBottles:
            def session_state(self, identity: str) -> dict[str, bool]:
                del identity
                return {"running": False}

        count = running_children_of("p-id", FakeBottles())
        self.assertEqual(count, 0)


# --- approvals: launch-bottle refused from terminal ---

class TestApprovalsLaunchBottle(unittest.TestCase):
    def test_allow_raises_for_launch_bottle(self):
        from bot_bottle.approvals import ALLOW_ONCE, Request, answer
        from bot_bottle.contract.supervise import TOOL_LAUNCH_BOTTLE
        req = Request(id="x", bottle="b", session="s", tool=TOOL_LAUNCH_BOTTLE,
                      justification="j", proposed='{}', current="")
        client = SimpleNamespace(supervise_respond=_no_op)
        with self.assertRaises(ValueError):
            answer(client, req, ALLOW_ONCE, via="terminal")

    def test_deny_works_for_launch_bottle(self):
        from bot_bottle.approvals import DENY, Request, answer
        from bot_bottle.contract.supervise import TOOL_LAUNCH_BOTTLE
        calls: list[dict[str, Any]] = []
        req = Request(id="x", bottle="b", session="s", tool=TOOL_LAUNCH_BOTTLE,
                      justification="j",
                      proposed=json.dumps({"agent": "a", "prompt": "p", "bottles": [],
                                           "label": ""}),
                      current="")

        class FakeClient:
            def supervise_respond(self, *_a: Any, **kw: Any) -> None:
                calls.append(kw)

        answer(FakeClient(), req, DENY, via="terminal")
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
