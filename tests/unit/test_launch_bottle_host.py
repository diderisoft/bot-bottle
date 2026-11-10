"""Unit: launch-bottle on the host controller — vetting a request before the
card, the card itself, the trimmed run bottle, and the phone's answer that
launches the child."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.approvals import Request
from bot_bottle.contract.supervise import TOOL_LAUNCH_BOTTLE
from bot_bottle.control.hostctl import launch_bottle as lb
from bot_bottle.control.hostctl import remote_actions
from bot_bottle.manifest.util import ManifestError

ARRIVED = "2026-01-02T03:04:05+00:00"
ME = "bot_bottle.control.hostctl.manifest_elements"

Patches = list[tuple[str, dict[str, Any]]]


def _start(case: unittest.TestCase, patches: Patches) -> None:
    for target, kw in patches:
        patcher = mock.patch(target, **kw)
        patcher.start()
        case.addCleanup(patcher.stop)


def _proposal(**over: Any) -> str:
    d = {"agent": "coder", "prompt": "write tests", "bottles": ["app-dev"], "label": "t"}
    d.update(over)
    return json.dumps(d)


def _request(proposed: str | None = None, justification: str = "needs a helper") -> Request:
    return Request(id="r1", bottle="b1", session="parent-id", tool=TOOL_LAUNCH_BOTTLE,
                   justification=justification,
                   proposed=_proposal() if proposed is None else proposed,
                   current="", arrived=ARRIVED, orchestrator="o")


def _elem(key: str) -> dict[str, str]:
    return {"key": key, "kind": key.split(":", 1)[0], "name": key.split(":", 1)[1]}


class _Index:
    def __init__(self, agents: tuple[str, ...] = ("coder",),
                 bottles: tuple[str, ...] = ("app-dev",)) -> None:
        self.agents = agents
        self.all_bottle_names = list(bottles)

    def require_agent(self, name: str) -> None:
        if name not in self.agents:
            raise ManifestError(f"agent {name!r} not defined")

    def bottle_dirs(self) -> list[tuple[Path, bool]]:
        return []


class TestVetLaunchBottle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.child = False
        self.running = 0
        self.index: _Index | Exception = _Index()
        self.chain: list[dict[str, Path]] = []
        self.conns: dict[str, Any] = {}
        self.known_bottles = {"app-dev"}

        def load_chain(name: str, _dirs: Any) -> tuple[None, list[Any]]:
            if name not in self.known_bottles:
                raise ManifestError(f"no bottle {name}")
            return None, []

        def resolve(*_a: Any, **_kw: Any) -> _Index:
            if isinstance(self.index, Exception):
                raise self.index
            return self.index

        def is_child(_session: str) -> bool:
            return self.child

        def running(_session: str, _bottles: Any) -> int:
            return self.running

        def chain_files(_names: Any, _dirs: Any) -> list[dict[str, Path]]:
            return self.chain

        _start(self, [
            ("bot_bottle.control.hostctl.children.is_launch_bottle_child",
             {"side_effect": is_child}),
            ("bot_bottle.control.hostctl.children.running_children_of", {"side_effect": running}),
            ("bot_bottle.manifest.ManifestIndex.resolve", {"side_effect": resolve}),
            ("bot_bottle.manifest.loader.load_bottle_chain", {"side_effect": load_chain}),
            ("bot_bottle.forge_connections.connections", {"side_effect": lambda: self.conns}),
            (f"{ME}._chain_files", {"side_effect": chain_files}),
        ])

    def _bottle_file(self, text: str) -> None:
        path = self.tmp / f"b{len(self.chain)}.md"
        path.write_text(text, encoding="utf-8")
        self.chain.append({"path": path})

    def _vet(self, proposed: str | None = None) -> str:
        return lb.vet_launch_bottle(_request(proposed), bottles=object())

    def test_a_meetable_request_is_empty(self):
        self.assertEqual("", self._vet())

    def test_a_bad_proposal_says_why(self):
        self.assertIn("valid JSON", self._vet("{nope"))

    def test_a_child_cant_launch_its_own(self):
        self.child = True
        self.assertIn("nested launch-bottle", self._vet())

    def test_the_children_cap(self):
        self.running = 3
        self.assertIn("3 running children; the cap is 3", self._vet())

    def test_an_unreadable_manifest(self):
        self.index = ManifestError("broken")
        self.assertIn("can't read the manifest: broken", self._vet())

    def test_an_unknown_agent(self):
        self.assertIn("agent 'ghost' not defined", self._vet(_proposal(agent="ghost")))

    def test_an_unknown_bottle_lists_the_defined_ones(self):
        msg = self._vet(_proposal(bottles=["nope"]))
        self.assertIn("bottle 'nope' not defined. Available: app-dev", msg)

    def test_a_repo_on_an_unconnected_forge(self):
        self._bottle_file("---\ngit_gate:\n  repos:\n    - git.example.com/acme/app:write\n---\n")
        msg = self._vet()
        self.assertIn("forge 'git.example.com' isn't connected", msg)

    def test_a_repo_on_a_connected_forge(self):
        self._bottle_file("---\ngit_gate:\n  repos:\n    - git.example.com/acme/app\n---\n")
        self.chain.append({"path": self.tmp / "missing.md"})  # unreadable: skipped
        self.conns = {"x": SimpleNamespace(host="git.example.com:443")}
        self.assertEqual("", self._vet())

    def test_no_connections_on_record_counts_as_none(self):
        from bot_bottle.forge_connections import ForgeConnectionError

        def broken() -> dict[str, Any]:
            raise ForgeConnectionError("unreadable")
        self._bottle_file("---\ngit_gate:\n  repos:\n    - git.example.com/acme/app\n---\n")
        with mock.patch("bot_bottle.forge_connections.connections", side_effect=broken):
            self.assertIn("isn't connected", self._vet())


class TestLaunchBottleAttention(unittest.TestCase):
    def test_lines_and_the_one_option(self):
        lines, options = lb.launch_bottle_attention(_request(), [], [])
        self.assertEqual(["launch_bottle"], options)
        self.assertEqual(["agent: coder", "bottles: app-dev", "prompt: write tests",
                          "label: t", "why: needs a helper"], lines)

    def test_a_long_prompt_is_cut(self):
        lines, _ = lb.launch_bottle_attention(
            _request(_proposal(prompt="x" * 250, bottles=[], label=""), justification=" "), [], [])
        self.assertEqual(["agent: coder", f"prompt: {'x' * 200}…"], lines)

    def test_what_the_parent_lacks_is_listed_as_extra(self):
        child = [_elem(k) for k in ("egress:a.com", "repo:app", "env:TOKEN", "forge:gh",
                                    "other:thing", "env:B", "env:C", "resources:memory")]
        lines, options = lb.launch_bottle_attention(_request(), [_elem("env:C")], child)
        self.assertEqual(["launch_bottle", "extra:egress:a.com", "extra:repo:app",
                          "extra:env:TOKEN", "extra:forge:gh", "extra:other:thing",
                          "extra:env:B"], options)
        self.assertEqual("extra (6): egress a.com (not in parent), repo app (not in parent), "
                         "env TOKEN (not in parent), forge gh (not in parent), "
                         "other:thing (not in parent) (+1 more)", lines[-1])

    def test_a_bad_proposal(self):
        lines, options = lb.launch_bottle_attention(_request("[]"), [], [])
        self.assertEqual([], options)
        self.assertIn("bad proposal", lines[0])

    def test_card_dispatches_to_it(self):
        card = remote_actions.card(_request())
        assert card is not None
        lines, options = card
        self.assertEqual(["launch_bottle"], options)
        self.assertEqual("agent: coder", lines[0])


class TestTrimmedBottleText(unittest.TestCase):
    def test_only_what_is_within_the_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            near = Path(tmp) / "near.md"
            near.write_text("---\nenv:\n  A: one\n  B: two\nresources:\n  memory: 2g\n---\n",
                            encoding="utf-8")
            far = Path(tmp) / "far.md"
            far.write_text("---\nenv:\n  A: zero\negress:\n  routes:\n    - host: a.example.com\n"
                           "resources:\n  memory: 9g\n---\n", encoding="utf-8")
            chain = [{"path": near}, {"path": Path(tmp) / "gone.md"}, {"path": far}]
            with mock.patch(f"{ME}._chain_files", return_value=chain):
                text = lb.trimmed_bottle_text(("app-dev",), [],
                                              [_elem("env:A"), _elem("egress:a.example.com")])
        self.assertTrue(text.startswith("---\n") and text.endswith("---\n"))
        self.assertIn("A: one", text)  # the nearest file's
        self.assertNotIn("zero", text)
        self.assertNotIn("B:", text)
        self.assertIn("a.example.com", text)
        self.assertIn("memory: 2g", text)
        self.assertNotIn("9g", text)


class _Orchestrator:
    def __init__(self, already: bool = False) -> None:
        self.already = already
        self.responses: list[tuple[str, dict[str, Any]]] = []

    def supervise_respond(self, request_id: str, **kw: Any) -> None:
        from bot_bottle.control.client import AlreadyAnswered
        if self.already:
            raise AlreadyAnswered("answered")
        self.responses.append((request_id, kw))


class TestLaunchBottleAnswer(unittest.TestCase):
    def setUp(self):
        self.orch = _Orchestrator()
        self.launched: list[tuple[Any, str]] = []
        self.parents: list[tuple[str, str]] = []
        self.nudges: list[tuple[Any, ...]] = []
        self.child_elems = [_elem("env:A"), _elem("env:SECRET")]
        self.trimmed_keys: list[str] = []

        def trimmed(_names: Any, _dirs: Any, within: list[dict[str, str]]) -> str:
            self.trimmed_keys = [e["key"] for e in within]
            return "---\nenv:\n  A: one\n---\n"

        def child_elements(_names: Any, _dirs: Any) -> list[dict[str, str]]:
            return self.child_elems

        def record_parent(child: str, parent: str) -> None:
            self.parents.append((child, parent))

        def nudge(*args: Any) -> None:
            self.nudges.append(args)

        _start(self, [
            ("bot_bottle.manifest.ManifestIndex.resolve", {"return_value": _Index()}),
            ("bot_bottle.control.hostctl.manifest_view._live_policy", {"return_value": None}),
            (f"{ME}.elements", {"return_value": [_elem("env:A")]}),
            (f"{ME}.bottle_elements", {"side_effect": child_elements}),
            ("bot_bottle.control.hostctl.launch_bottle.trimmed_bottle_text",
             {"side_effect": trimmed}),
            ("bot_bottle.bottle_state.record_parent", {"side_effect": record_parent}),
            ("bot_bottle.control.hostctl.nudge.nudge_if_waited", {"side_effect": nudge}),
        ])

    def _launch(self, req: Any, role: str) -> dict[str, str]:
        self.launched.append((req, role))
        return {"bottle": "child-id"}

    def _orchestrator(self, _url: str) -> _Orchestrator:
        return self.orch

    def _answer(self, request: Request | None = None, since: str = ARRIVED,
                bottles: Any = None, approved: Any = None) -> dict[str, Any]:
        request = request or _request()
        msg: dict[str, Any] = {"agent_id": "parent-id", "request_id": 7, "since": since}
        if approved is not None:
            msg["approved_extras"] = approved
        bottles = bottles if bottles is not None else SimpleNamespace(launch=self._launch)
        return asyncio.run(lb.launch_bottle_answer(
            msg, {"parent-id": request}, self._orchestrator, bottles))

    def test_launches_the_child_and_approves(self):
        reply = self._answer()
        self.assertNotIn("error", reply)
        self.assertEqual({"child_identity": "child-id", "agent": "coder", "label": "t",
                          "approved": 0, "removed": 1},
                         {k: reply[k] for k in
                          ("child_identity", "agent", "label", "approved", "removed")})
        self.assertEqual(["env:A"], self.trimmed_keys)  # the extra is left out
        self.assertEqual(1, len(self.launched))
        req = self.launched[0][0]
        self.assertEqual("coder", req.agent)
        self.assertEqual("app-dev", req.bottles[0])
        self.assertTrue(req.headless)
        self.assertEqual("---\nenv:\n  A: one\n---\n", req.run_bottle)
        self.assertEqual([("child-id", "parent-id")], self.parents)
        self.assertEqual(1, len(self.orch.responses))
        rid, kw = self.orch.responses[0]
        self.assertEqual("r1", rid)
        self.assertEqual("approve", kw["decision"])
        self.assertEqual("launched coder; identity=child-id; 1 extra(s) removed",
                         kw["notes"])
        self.assertEqual("approved", self.nudges[0][1])

    def test_no_longer_waiting(self):
        self.assertIn("no longer waiting", self._answer(since="2020-01-01T00:00:00+00:00")["error"])
        other = Request(id="r", bottle="b", session="s", tool="add-repo", justification="",
                        proposed="", current="", arrived=ARRIVED)
        self.assertIn("no longer waiting", self._answer(request=other)["error"])
        self.assertEqual([], self.launched)

    def test_a_bad_proposal(self):
        self.assertIn("valid JSON", self._answer(request=_request("{nope"))["error"])

    def test_building_the_trimmed_bottle_fails(self):
        with mock.patch(f"{ME}.bottle_elements", side_effect=RuntimeError("boom")):
            reply = self._answer()
        self.assertIn("building the trimmed bottle failed", reply["error"])
        self.assertEqual([], self.launched)

    def test_a_host_that_cant_launch(self):
        self.assertIn("can't launch bottles", self._answer(bottles=object())["error"])

    def test_the_launch_fails(self):
        def fail(_req: Any, _role: str) -> dict[str, str]:
            raise RuntimeError("no room")
        reply = self._answer(bottles=SimpleNamespace(launch=fail))
        self.assertEqual("launching the child bottle failed: no room", reply["error"])
        self.assertEqual([], self.orch.responses)

    def test_answered_elsewhere_meanwhile(self):
        self.orch.already = True
        self.child_elems = [_elem("env:A")]
        reply = self._answer()
        self.assertEqual(["the request was answered elsewhere meanwhile"], reply["warnings"])
        self.assertEqual(0, reply["removed"])

    def test_no_identity_back_records_no_parent(self):
        def no_identity(_req: Any, _role: str) -> dict[str, str]:
            return {}
        reply = self._answer(bottles=SimpleNamespace(launch=no_identity))
        self.assertEqual("", reply["child_identity"])
        self.assertEqual([], self.parents)
        self.assertEqual("launched coder; 1 extra(s) removed",
                         self.orch.responses[0][1]["notes"])


    def test_an_approved_extra_goes_in(self):
        reply = self._answer(approved=["env:SECRET"])
        self.assertEqual(["env:A", "env:SECRET"], self.trimmed_keys)
        self.assertEqual((1, 0), (reply["approved"], reply["removed"]))
        self.assertEqual("launched coder; identity=child-id; 1 extra(s) approved",
                         self.orch.responses[0][1]["notes"])

    def test_only_the_childs_own_extras_can_be_approved(self):
        # A key the child's bottles don't declare can't be smuggled in.
        reply = self._answer(approved=["env:ROOT_TOKEN", "egress:evil.example", 7])
        self.assertEqual(["env:A"], self.trimmed_keys)
        self.assertEqual((0, 1), (reply["approved"], reply["removed"]))

    def test_malformed_approvals_approve_nothing(self):
        for approved in ("env:SECRET", {"env:SECRET": True}, 1):
            with self.subTest(approved=approved):
                reply = self._answer(approved=approved)
                self.assertNotIn("error", reply)
                self.assertEqual(["env:A"], self.trimmed_keys)


class TestSpecialCard(unittest.TestCase):
    """`special_card` for a launch-bottle: refused before the card when it
    can't be met, else the card with what the parent lacks."""

    def setUp(self):
        self.orch = _Orchestrator()
        _start(self, [
            ("bot_bottle.control.hostctl.nudge.nudge_if_waited", {}),
            ("bot_bottle.manifest.ManifestIndex.resolve", {"return_value": _Index()}),
            ("bot_bottle.control.hostctl.manifest_view._live_policy", {"return_value": None}),
            (f"{ME}.elements", {"return_value": []}),
            (f"{ME}.bottle_elements", {"return_value": [_elem("env:SECRET")]}),
        ])

    def _orchestrator(self, _url: str) -> _Orchestrator:
        return self.orch

    def _card(self) -> tuple[list[str], list[str]]:
        card = asyncio.run(remote_actions.special_card(_request(), self._orchestrator,
                                                       bottles=object()))
        assert card is not None
        return card

    def test_a_refusal_answers_and_shows_no_card(self):
        with mock.patch.object(lb, "vet_launch_bottle", return_value="cap reached"):
            self.assertEqual(([], []), self._card())
        self.assertEqual(1, len(self.orch.responses))
        kw = self.orch.responses[0][1]
        self.assertEqual(("reject", "cap reached"), (kw["decision"], kw["notes"]))

    def test_a_refusal_answered_elsewhere(self):
        self.orch.already = True
        with mock.patch.object(lb, "vet_launch_bottle", return_value="cap reached"):
            self.assertEqual(([], []), self._card())

    def test_a_failed_refusal_shows_the_card(self):
        self.orch.supervise_respond = mock.Mock(side_effect=RuntimeError("down"))
        with mock.patch.object(lb, "vet_launch_bottle", return_value="cap reached"):
            lines, options = self._card()
        self.assertEqual(["launch_bottle", "extra:env:SECRET"], options)
        self.assertIn("extra (1): env SECRET (not in parent)", lines)

    def test_a_meetable_request_shows_the_card(self):
        with mock.patch.object(lb, "vet_launch_bottle", return_value=""):
            lines, options = self._card()
        self.assertEqual(["launch_bottle", "extra:env:SECRET"], options)
        self.assertIn("extra (1): env SECRET (not in parent)", lines)

    def test_the_card_without_the_comparison(self):
        with mock.patch.object(lb, "vet_launch_bottle", return_value=""), \
             mock.patch(f"{ME}.bottle_elements", side_effect=RuntimeError("boom")):
            lines, _ = self._card()
        self.assertFalse(any(line.startswith("extra") for line in lines))


if __name__ == "__main__":
    unittest.main()
