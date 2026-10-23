"""Unit: a bottle's `audit:` block, its merging, and where it ends up."""

from __future__ import annotations

import unittest

from bot_bottle.egress.service import egress_render_routes
from bot_bottle.gateway.egress.schema import load_config
from bot_bottle.manifest import ManifestError
from bot_bottle.manifest.bottle import ManifestAudit, ManifestBottle
from bot_bottle.manifest.extends import merge_bottles_runtime, resolve_bottles


class Parse(unittest.TestCase):
    def test_either_key_alone_is_fine(self) -> None:
        self.assertEqual(ManifestAudit(firehose="metadata"),
                         ManifestAudit.from_dict("b", {"firehose": "metadata"}))
        self.assertEqual(ManifestAudit(delivery="required"),
                         ManifestAudit.from_dict("b", {"delivery": "required"}))
        self.assertEqual(ManifestAudit(), ManifestAudit.from_dict("b", {}))

    def test_bad_values_and_keys_are_refused(self) -> None:
        for raw in ({"delivery": "sometimes"}, {"firehose": "none"}, {"bodies": False}):
            with self.subTest(raw=raw), self.assertRaises(ManifestError):
                ManifestAudit.from_dict("b", raw)

    def test_a_bottle_reads_its_audit_block(self) -> None:
        bottle = ManifestBottle.from_dict("b", {"audit": {"firehose": "metadata"}})
        self.assertEqual("metadata", bottle.audit.firehose)


class Merge(unittest.TestCase):
    def test_a_later_bottle_overrides_only_the_fields_it_sets(self) -> None:
        base = ManifestBottle.from_dict("base", {"audit": {"delivery": "required",
                                                           "firehose": "metadata"}})
        child = ManifestBottle.from_dict("child", {"audit": {"delivery": "best_effort"}})
        merged = merge_bottles_runtime([base, child])
        self.assertEqual(ManifestAudit(delivery="best_effort", firehose="metadata"),
                         merged.audit)

    def test_extends_inherits_the_audit_block(self) -> None:
        bottles = resolve_bottles({
            "parent": {"audit": {"firehose": "metadata"}},
            "child": {"extends": "parent", "audit": {"delivery": "required"}},
        })
        self.assertEqual(ManifestAudit(delivery="required", firehose="metadata"),
                         bottles["child"].audit)


class Policy(unittest.TestCase):
    def test_the_modes_reach_the_gateway_through_the_policy(self) -> None:
        config = load_config(egress_render_routes((), audit_delivery="required",
                                                  firehose="metadata"))
        self.assertEqual(("required", "metadata"), (config.audit_delivery, config.firehose))

    def test_defaults_leave_the_policy_unchanged(self) -> None:
        self.assertEqual("routes: []\n", egress_render_routes(()))


if __name__ == "__main__":
    unittest.main()
