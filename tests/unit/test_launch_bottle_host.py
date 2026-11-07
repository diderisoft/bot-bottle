"""Unit: launch-bottle on the host controller — vetting a request before the
card, the card itself, the trimmed run bottle, and the phone's answer that
launches the child."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot_bottle.approvals import Request
from bot_bottle.contract.supervise import TOOL_LAUNCH_BOTTLE
from bot_bottle.control.hostctl import launch_bottle as lb
from bot_bottle.control.hostctl import remote_actions
from bot_bottle.manifest.util import ManifestError

ARRIVED = "2026-01-02T03:04:05+00:00"
ME = "bot_bottle.control.hostctl.manifest_elements"


def _proposal(**over):
    d = {"agent": "coder", "prompt": "write tests", "bottles": ["app-dev"], "label": "t"}
    d.update(over)
    return json.dumps(d)


def _request(proposed=None, justification="needs a helper"):
    return Request(id="r1", bottle="b1", session="parent-id", tool=TOOL_LAUNCH_BOTTLE,
                   justification=justification,
                   proposed=_proposal() if proposed is None else proposed,
                   current="", arrived=ARRIVED, orchestrator="o")


def _elem(key):
    return {"key": key, "kind": key.split(":", 1)[0], "name": key.split(":", 1)[1]}


class _Index:
    def __init__(self, agents=("coder",), bottles=("app-dev",)):
        self.agents = agents
        self.all_bottle_names = list(bottles)

    def require_agent(self, name):
        if name not in self.agents:
            raise ManifestError(f"agent {name!r} not defined")

    def bottle_dirs(self):
        return []


class TestVetLaunchBottle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.child = False
        self.running = 0
        self.index = _Index()
        self.chain = []
        self.conns = {}
        self.known_bottles = {"app-dev"}

        def load_chain(name, _dirs):
            if name not in self.known_bottles:
                raise ManifestError(f"no bottle {name}")
            return None, []

        def resolve(*_a, **_kw):
            if isinstance(self.index, Exception):
                raise self.index
            return self.index

        for target, kw in (
            ("bot_bottle.control.hostctl.children.is_launch_bottle_child",
             {"side_effect": lambda _s: self.child}),
            ("bot_bottle.control.hostctl.children.running_children_of",
             {"side_effect": lambda _s, _b: self.running}),
            ("bot_bottle.manifest.ManifestIndex.resolve", {"side_effect": resolve}),
            ("bot_bottle.manifest.loader.load_bottle_chain", {"side_effect": load_chain}),
            ("bot_bottle.forge_connections.connections", {"side_effect": lambda: self.conns}),
            (f"{ME}._chain_files", {"side_effect": lambda _n, _d: self.chain}),
        ):
            patcher = mock.patch(target, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _bottle_file(self, text):
        path = Path(self.tmp.name) / f"b{len(self.chain)}.md"
        path.write_text(text, encoding="utf-8")
        self.chain.append({"path": path})

    def _vet(self, proposed=None):
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
        self._bottle_file("---\ngit-gate:\n  repos:\n    - git.example.com/acme/app:write\n---\n")
        msg = self._vet()
        self.assertIn("forge 'git.example.com' isn't connected", msg)

    def test_a_repo_on_a_connected_forge(self):
        self._bottle_file("---\ngit-gate:\n  repos:\n    - git.example.com/acme/app\n---\n")
        self.chain.append({"path": Path(self.tmp.name) / "missing.md"})  # unreadable: skipped
        self.conns = {"x": SimpleNamespace(host="git.example.com:443")}
        self.assertEqual("", self._vet())

    def test_no_connections_on_record_counts_as_none(self):
        from bot_bottle.forge_connections import ForgeConnectionError

        def broken():
            raise ForgeConnectionError("unreadable")
        self._bottle_file("---\ngit-gate:\n  repos:\n    - git.example.com/acme/app\n---\n")
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

    def test_what_the_parent_lacks_is_listed_as_removed(self):
        child = [_elem(k) for k in ("egress:a.com", "repo:app", "env:TOKEN", "forge:gh",
                                    "other:thing", "env:B", "env:C", "resources:memory")]
        lines, _ = lb.launch_bottle_attention(_request(), [_elem("env:C")], child)
        self.assertEqual("removed (6): egress a.com (not in parent), repo app (not in parent), "
                         "env TOKEN (not in parent), forge gh (not in parent), "
                         "other:thing (not in parent) (+1 more)", lines[-1])

    def test_a_bad_proposal(self):
        lines, options = lb.launch_bottle_attention(_request("[]"), [], [])
        self.assertEqual([], options)
        self.assertIn("bad proposal", lines[0])

    def test_card_dispatches_to_it(self):
        lines, options = remote_actions.card(_request())
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
    def __init__(self, already=False):
        self.already = already
        self.responses = []

    def supervise_respond(self, request_id, **kw):
        from bot_bottle.control.client import AlreadyAnswered
        if self.already:
            raise AlreadyAnswered("answered")
        self.responses.append((request_id, kw))


class TestLaunchBottleAnswer(unittest.TestCase):
    def setUp(self):
        self.orch = _Orchestrator()
        self.launched = []
        self.parents = []
        self.nudges = []
        self.child_elems = [_elem("env:A"), _elem("env:SECRET")]
        for target, kw in (
            ("bot_bottle.manifest.ManifestIndex.resolve", {"return_value": _Index()}),
            ("bot_bottle.control.hostctl.manifest_view._live_policy", {"return_value": None}),
            (f"{ME}.elements", {"return_value": [_elem("env:A")]}),
            (f"{ME}.bottle_elements", {"side_effect": lambda _n, _d: self.child_elems}),
            ("bot_bottle.control.hostctl.launch_bottle.trimmed_bottle_text",
             {"return_value": "---\nenv:\n  A: one\n---\n"}),
            ("bot_bottle.bottle_state.record_parent",
             {"side_effect": lambda c, p: self.parents.append((c, p))}),
            ("bot_bottle.control.hostctl.nudge.nudge_if_waited",
             {"side_effect": lambda *a: self.nudges.append(a)}),
        ):
            patcher = mock.patch(target, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _launch(self, req, role):
        self.launched.append((req, role))
        return {"bottle": "child-id"}

    def _answer(self, request=None, since=ARRIVED, bottles=None):
        request = request or _request()
        msg = {"agent_id": "parent-id", "request_id": 7, "since": since}
        bottles = bottles if bottles is not None else SimpleNamespace(launch=self._launch)
        return asyncio.run(lb.launch_bottle_answer(
            msg, {"parent-id": request}, lambda _o: self.orch, bottles))

    def test_launches_the_child_and_approves(self):
        reply = self._answer()
        self.assertNotIn("error", reply)
        self.assertEqual({"child_identity": "child-id", "agent": "coder", "label": "t",
                          "removed": 1}, {k: reply[k] for k in
                                          ("child_identity", "agent", "label", "removed")})
        (req, _role), = self.launched
        self.assertEqual("coder", req.agent)
        self.assertEqual("app-dev", req.bottles[0])
        self.assertTrue(req.headless)
        self.assertEqual("---\nenv:\n  A: one\n---\n", req.run_bottle)
        self.assertEqual([("child-id", "parent-id")], self.parents)
        (rid, kw), = self.orch.responses
        self.assertEqual("r1", rid)
        self.assertEqual("approve", kw["decision"])
        self.assertEqual("launched coder; identity=child-id; 1 extra element(s) removed",
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
        def fail(_req, _role):
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
        reply = self._answer(bottles=SimpleNamespace(launch=lambda _r, _o: {}))
        self.assertEqual("", reply["child_identity"])
        self.assertEqual([], self.parents)
        self.assertEqual("launched coder; 1 extra element(s) removed",
                         self.orch.responses[0][1]["notes"])


class TestSpecialCard(unittest.TestCase):
    """`special_card` for a launch-bottle: refused before the card when it
    can't be met, else the card with what the parent lacks."""

    def setUp(self):
        self.orch = _Orchestrator()
        for target, kw in (
            ("bot_bottle.control.hostctl.nudge.nudge_if_waited", {}),
            ("bot_bottle.manifest.ManifestIndex.resolve", {"return_value": _Index()}),
            ("bot_bottle.control.hostctl.manifest_view._live_policy", {"return_value": None}),
            (f"{ME}.elements", {"return_value": []}),
            (f"{ME}.bottle_elements", {"return_value": [_elem("env:SECRET")]}),
        ):
            patcher = mock.patch(target, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _card(self):
        return asyncio.run(remote_actions.special_card(_request(), lambda _o: self.orch,
                                                       bottles=object()))

    def test_a_refusal_answers_and_shows_no_card(self):
        with mock.patch.object(lb, "vet_launch_bottle", return_value="cap reached"):
            self.assertEqual(([], []), self._card())
        (_, kw), = self.orch.responses
        self.assertEqual(("reject", "cap reached"), (kw["decision"], kw["notes"]))

    def test_a_refusal_answered_elsewhere(self):
        self.orch.already = True
        with mock.patch.object(lb, "vet_launch_bottle", return_value="cap reached"):
            self.assertEqual(([], []), self._card())

    def test_a_failed_refusal_shows_the_card(self):
        self.orch.supervise_respond = mock.Mock(side_effect=RuntimeError("down"))
        with mock.patch.object(lb, "vet_launch_bottle", return_value="cap reached"):
            lines, options = self._card()
        self.assertEqual(["launch_bottle"], options)
        self.assertIn("removed (1): env SECRET (not in parent)", lines)

    def test_a_meetable_request_shows_the_card(self):
        with mock.patch.object(lb, "vet_launch_bottle", return_value=""):
            lines, options = self._card()
        self.assertEqual(["launch_bottle"], options)
        self.assertIn("removed (1): env SECRET (not in parent)", lines)

    def test_the_card_without_the_comparison(self):
        with mock.patch.object(lb, "vet_launch_bottle", return_value=""), \
             mock.patch(f"{ME}.bottle_elements", side_effect=RuntimeError("boom")):
            lines, _ = self._card()
        self.assertFalse(any(line.startswith("removed") for line in lines))


if __name__ == "__main__":
    unittest.main()
