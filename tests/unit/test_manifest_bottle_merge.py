"""Unit: runtime bottle composition (issue #269).

Tests for merge_bottles_runtime and ManifestIndex.load_for_agent with
the new bottle_names parameter.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from bot_bottle.manifest import ManifestBottle, ManifestError, ManifestIndex
from bot_bottle.manifest_extends import merge_bottles_runtime


def _index(bottles: dict[str, object], agents: dict[str, object]) -> ManifestIndex:
    return ManifestIndex.from_json_obj({"bottles": bottles, "agents": agents})


def _bottle(**kwargs: object) -> ManifestBottle:
    return ManifestBottle.from_dict("test", kwargs)


class TestMergeBottlesRuntime(unittest.TestCase):
    def test_single_bottle_returns_as_is(self):
        b = _bottle(env={"FOO": "1"})
        result = merge_bottles_runtime([b])
        self.assertEqual({"FOO": "1"}, dict(result.env))

    def test_env_later_wins(self):
        base = _bottle(env={"FOO": "base", "ONLY_BASE": "x"})
        override = _bottle(env={"FOO": "override", "ONLY_OVERRIDE": "y"})
        result = merge_bottles_runtime([base, override])
        self.assertEqual("override", result.env["FOO"])
        self.assertEqual("x", result.env["ONLY_BASE"])
        self.assertEqual("y", result.env["ONLY_OVERRIDE"])

    def test_egress_routes_concatenated(self):
        from bot_bottle.manifest_egress import ManifestEgressConfig, ManifestEgressRoute
        r1 = ManifestEgressRoute(Host="api.a.com")
        r2 = ManifestEgressRoute(Host="api.b.com")
        base = ManifestBottle(egress=ManifestEgressConfig(routes=(r1,)))
        override = ManifestBottle(egress=ManifestEgressConfig(routes=(r2,)))
        result = merge_bottles_runtime([base, override])
        hosts = [r.Host for r in result.egress.routes]
        self.assertIn("api.a.com", hosts)
        self.assertIn("api.b.com", hosts)

    def test_supervise_later_wins(self):
        base = _bottle(supervise=True)
        override = _bottle(supervise=False)
        result = merge_bottles_runtime([base, override])
        self.assertFalse(result.supervise)

    def test_supervise_survives_a_later_bottle_that_omits_the_key(self):
        disabled = _bottle(supervise=False)
        quiet = _bottle(env={"X": "1"})
        self.assertFalse(merge_bottles_runtime([disabled, quiet]).supervise)

    def test_supervise_explicit_true_overrides_earlier_false(self):
        disabled = _bottle(supervise=False)
        enabled = _bottle(supervise=True)
        self.assertTrue(merge_bottles_runtime([disabled, enabled]).supervise)

    def test_nested_containers_survives_a_later_bottle_that_omits_the_key(self):
        """A bottle that never mentions nested_containers must not silently
        drop the capability: `--bottle with-docker --bottle claude-dev`."""
        enabled = _bottle(nested_containers=True)
        quiet = _bottle(env={"X": "1"})
        self.assertTrue(merge_bottles_runtime([enabled, quiet]).nested_containers)
        self.assertTrue(merge_bottles_runtime([quiet, enabled]).nested_containers)
        self.assertFalse(merge_bottles_runtime([quiet, quiet]).nested_containers)

    def test_nested_containers_explicit_false_overrides_earlier_true(self):
        """An explicit nested_containers: false in a later bottle must win
        over an earlier true (last-wins, presence-aware)."""
        enabled = _bottle(nested_containers=True)
        disabled = _bottle(nested_containers=False)
        self.assertFalse(merge_bottles_runtime([enabled, disabled]).nested_containers)

    def test_nested_containers_explicit_true_overrides_earlier_false(self):
        enabled = _bottle(nested_containers=True)
        disabled = _bottle(nested_containers=False)
        self.assertTrue(merge_bottles_runtime([disabled, enabled]).nested_containers)

    def test_extended_boolean_values_remain_explicit_at_runtime(self):
        idx = _index(
            bottles={
                "enabled": {"nested_containers": True, "supervise": True},
                "parent": {},
                "disabled_child": {
                    "extends": "parent",
                    "nested_containers": False,
                    "supervise": False,
                },
            },
            agents={"impl": {"bottle": "enabled", "skills": [], "prompt": ""}},
        )
        result = idx.load_for_agent(
            "impl", ("enabled", "disabled_child")
        ).bottle
        self.assertFalse(result.nested_containers)
        self.assertFalse(result.supervise)

    def test_three_bottles_merged_left_to_right(self):
        b1 = _bottle(env={"A": "1", "B": "1", "C": "1"})
        b2 = _bottle(env={"B": "2", "C": "2"})
        b3 = _bottle(env={"C": "3"})
        result = merge_bottles_runtime([b1, b2, b3])
        self.assertEqual("1", result.env["A"])
        self.assertEqual("2", result.env["B"])
        self.assertEqual("3", result.env["C"])

    def test_empty_list_raises(self):
        with self.assertRaises(ValueError):
            merge_bottles_runtime([])


class TestLoadForAgentWithBottleNames(unittest.TestCase):
    def test_bottle_names_override_agent_bottle(self):
        idx = _index(
            bottles={
                "base": {"env": {"X": "base"}},
                "override": {"env": {"X": "override"}},
            },
            agents={"impl": {"bottle": "base", "skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ("override",))
        self.assertEqual("override", m.bottle.env["X"])

    def test_bottle_names_merged_in_order(self):
        idx = _index(
            bottles={
                "a": {"env": {"X": "a", "A": "only-a"}},
                "b": {"env": {"X": "b", "B": "only-b"}},
            },
            agents={"impl": {"bottle": "a", "skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ("a", "b"))
        self.assertEqual("b", m.bottle.env["X"])
        self.assertEqual("only-a", m.bottle.env["A"])
        self.assertEqual("only-b", m.bottle.env["B"])

    def test_empty_bottle_names_uses_agent_bottle(self):
        idx = _index(
            bottles={"base": {"env": {"X": "base"}}},
            agents={"impl": {"bottle": "base", "skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ())
        self.assertEqual("base", m.bottle.env["X"])

    def test_no_bottle_and_no_bottle_names_raises(self):
        idx = _index(
            bottles={"base": {}},
            agents={"impl": {"skills": [], "prompt": ""}},
        )
        with self.assertRaises(ManifestError) as ctx:
            idx.load_for_agent("impl", ())
        self.assertIn("no 'bottle' field", str(ctx.exception))

    def test_unknown_bottle_name_raises(self):
        idx = _index(
            bottles={"base": {}},
            agents={"impl": {"bottle": "base", "skills": [], "prompt": ""}},
        )
        with self.assertRaises(ManifestError) as ctx:
            idx.load_for_agent("impl", ("nonexistent",))
        self.assertIn("nonexistent", str(ctx.exception))

    def test_agent_without_bottle_works_with_bottle_names(self):
        idx = _index(
            bottles={"base": {"env": {"X": "base"}}},
            agents={"impl": {"skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ("base",))
        self.assertEqual("base", m.bottle.env["X"])


class TestAllBottleNames(unittest.TestCase):
    def test_eager_mode_returns_bottle_names(self):
        idx = _index(
            bottles={"alpha": {}, "beta": {}, "gamma": {}},
            agents={"impl": {"bottle": "alpha", "skills": [], "prompt": ""}},
        )
        self.assertEqual(["alpha", "beta", "gamma"], idx.all_bottle_names)

    def test_lazy_mode_scans_files(self):
        home = Path(tempfile.mkdtemp(prefix="cb-home-"))
        orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            bottles_dir = home / ".bot-bottle" / "bottles"
            agents_dir = home / ".bot-bottle" / "agents"
            bottles_dir.mkdir(parents=True)
            agents_dir.mkdir(parents=True)
            (bottles_dir / "claude.md").write_text("---\n---\n")
            (bottles_dir / "dev.md").write_text("---\n---\n")
            (agents_dir / "impl.md").write_text("---\nbottle: claude\n---\n")
            idx = ManifestIndex.resolve(str(home))
            self.assertEqual(["claude", "dev"], idx.all_bottle_names)
        finally:
            if orig_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = orig_home
            shutil.rmtree(home, ignore_errors=True)


class TestAgentOptionalBottleMd(unittest.TestCase):
    """Agent file without bottle: works when bottle_names are provided at launch."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="cb-home-"))
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _write(self, rel: str, text: str) -> None:
        p = self.home / ".bot-bottle" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"))

    def test_agent_without_bottle_resolves_with_bottle_names(self):
        self._write("bottles/dev.md", "---\nenv:\n  X: dev\n---\n")
        self._write("agents/impl.md", "---\n---\nimpl agent.\n")
        idx = ManifestIndex.resolve(str(self.home))
        m = idx.load_for_agent("impl", ("dev",))
        self.assertEqual("dev", m.bottle.env["X"])

    def test_agent_without_bottle_fails_without_bottle_names(self):
        self._write("bottles/dev.md", "---\n---\n")
        self._write("agents/impl.md", "---\n---\nimpl agent.\n")
        idx = ManifestIndex.resolve(str(self.home))
        with self.assertRaises(ManifestError) as ctx:
            idx.load_for_agent("impl", ())
        self.assertIn("no 'bottle' field", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
