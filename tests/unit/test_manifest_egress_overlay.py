"""Egress routes across bottles: a later bottle's route for a host replaces
the earlier one, and `{host, blocked: true}` takes an inherited host away
(ADR 0015)."""

from __future__ import annotations

import unittest
from collections.abc import Mapping

from bot_bottle.manifest import ManifestError, ManifestIndex
from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.egress import ManifestEgressConfig, ManifestEgressRoute, overlay_egress
from bot_bottle.manifest.extends import merge_bottles_runtime


def _bottles(**bottles: object) -> Mapping[str, ManifestBottle]:
    return ManifestIndex.from_json_obj({
        "bottles": bottles, "agents": {"demo": {"skills": [], "prompt": ""}},
    }).bottles


def _hosts(bottle: ManifestBottle) -> list[str]:
    return [r.Host for r in bottle.egress.routes]


BASE = {"egress": {"routes": [{"host": "a.example.com"}, {"host": "b.example.com"},
                              {"host": "c.example.com"}]}}


class TestReplace(unittest.TestCase):
    def test_replacement_keeps_the_parents_position(self):
        b = _bottles(base=BASE, child={"extends": "base", "egress": {"routes": [
            {"host": "d.example.com"}, {"host": "b.example.com", "passthrough": True}]}})
        self.assertEqual(["a.example.com", "b.example.com", "c.example.com", "d.example.com"],
                         _hosts(b["child"]))
        self.assertFalse(b["child"].egress.routes[1].Inspect)

    def test_later_parent_wins_over_earlier_parent(self):
        b = _bottles(
            p1={"egress": {"routes": [{"host": "a.example.com"}]}},
            p2={"egress": {"routes": [{"host": "a.example.com", "passthrough": True}]}},
            child={"extends": ["p1", "p2"]})
        self.assertEqual(["a.example.com"], _hosts(b["child"]))
        self.assertFalse(b["child"].egress.routes[0].Inspect)

    def test_runtime_stack_later_wins(self):
        b = _bottles(base=BASE, extra={"egress": {"routes": [
            {"host": "c.example.com", "passthrough": True}]}})
        merged = merge_bottles_runtime([b["base"], b["extra"]])
        self.assertEqual(["a.example.com", "b.example.com", "c.example.com"], _hosts(merged))
        self.assertFalse(merged.egress.routes[2].Inspect)


class TestBlocked(unittest.TestCase):
    def test_block_drops_the_inherited_host(self):
        b = _bottles(base=BASE, child={"extends": "base", "egress": {"routes": [
            {"host": "B.example.com", "blocked": True}]}})
        self.assertEqual(["a.example.com", "c.example.com"], _hosts(b["child"]))
        self.assertEqual(("B.example.com",), b["child"].egress.Blocked)

    def test_block_with_nothing_to_remove_is_a_no_op(self):
        b = _bottles(dev={"egress": {"routes": [{"host": "x.example.com", "blocked": True}]}})
        self.assertEqual([], _hosts(b["dev"]))

    def test_grandchild_route_unblocks(self):
        b = _bottles(base=BASE,
                     mid={"extends": "base", "egress": {"routes": [
                         {"host": "a.example.com", "blocked": True}]}},
                     leaf={"extends": "mid", "egress": {"routes": [{"host": "a.example.com"}]}})
        self.assertEqual(["b.example.com", "c.example.com", "a.example.com"], _hosts(b["leaf"]))
        self.assertEqual((), b["leaf"].egress.Blocked)

    def test_block_applies_to_bottles_stacked_before_it(self):
        b = _bottles(base=BASE, narrow={"egress": {"routes": [
            {"host": "a.example.com", "blocked": True}]}})
        merged = merge_bottles_runtime([b["base"], b["narrow"]])
        self.assertEqual(["b.example.com", "c.example.com"], _hosts(merged))

    def test_block_with_other_keys_dies(self):
        with self.assertRaisesRegex(ManifestError, "takes only 'host'"):
            _bottles(dev={"egress": {"routes": [
                {"host": "a.example.com", "blocked": True, "passthrough": True}]}})

    def test_blocked_false_dies(self):
        with self.assertRaisesRegex(ManifestError, "blocked must be true"):
            _bottles(dev={"egress": {"routes": [{"host": "a.example.com", "blocked": False}]}})

    def test_blocked_without_host_dies(self):
        with self.assertRaisesRegex(ManifestError, "missing required string field 'host'"):
            _bottles(dev={"egress": {"routes": [{"blocked": True}]}})

    def test_routed_and_blocked_in_one_bottle_dies(self):
        with self.assertRaisesRegex(ManifestError, "duplicate host"):
            _bottles(dev={"egress": {"routes": [
                {"host": "a.example.com"}, {"host": "A.example.com", "blocked": True}]}})

    def test_document_keeps_the_block(self):
        b = _bottles(base=BASE, child={"extends": "base", "egress": {"routes": [
            {"host": "a.example.com", "blocked": True}]}})
        routes = bottle_document(b["child"])["egress"]["routes"]  # type: ignore[index]
        self.assertIn({"host": "a.example.com", "blocked": True}, routes)
        self.assertNotIn({"host": "a.example.com"}, routes)


class TestOverlayEgress(unittest.TestCase):
    def test_a_later_block_drops_an_earlier_block_duplicate(self):
        earlier = ManifestEgressConfig(Blocked=("a.example.com",))
        later = ManifestEgressConfig(Blocked=("A.example.com",))
        self.assertEqual(("A.example.com",), overlay_egress(earlier, later, 0).Blocked)

    def test_log_is_what_the_caller_says(self):
        earlier = ManifestEgressConfig(routes=(ManifestEgressRoute(Host="a"),), Log=1)
        self.assertEqual(2, overlay_egress(earlier, ManifestEgressConfig(), 2).Log)


if __name__ == "__main__":
    unittest.main()
