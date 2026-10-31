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
from bot_bottle.manifest.extends import merge_bottles_runtime


def _index(bottles: dict[str, object], agents: dict[str, object]) -> ManifestIndex:
    return ManifestIndex.from_json_obj({"bottles": bottles, "agents": agents})


def _bottle(**kwargs: object) -> ManifestBottle:
    return ManifestBottle.from_dict("test", kwargs)


def _git_repo(url: str) -> dict[str, object]:
    return {"url": url, "key": {"provider": "gitea", "forge_token_env": "TOK"}}


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
        from bot_bottle.manifest.egress import ManifestEgressConfig, ManifestEgressRoute
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
            agents={"impl": {"skills": [], "prompt": ""}},
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

    def test_git_repo_only_in_override_does_not_raise(self):
        # Regression for issue #457: override bottle declares a repo that the
        # base doesn't have → KeyError on base_repos_by_name[n].
        base = _bottle(env={"X": "base"})
        override = _bottle(**{"git-gate": {"repos": {"myrepo": _git_repo("ssh://git@example.com/repo.git")}}})
        result = merge_bottles_runtime([base, override])
        self.assertIn("myrepo", [e.Name for e in result.git])

    def test_git_repo_only_in_base_survives_override(self):
        base = _bottle(**{"git-gate": {"repos": {"myrepo": _git_repo("ssh://git@example.com/repo.git")}}})
        override = _bottle(env={"X": "override"})
        result = merge_bottles_runtime([base, override])
        self.assertIn("myrepo", [e.Name for e in result.git])

    def test_git_repo_override_wins_by_name(self):
        base = _bottle(**{"git-gate": {"repos": {"myrepo": _git_repo("ssh://git@base.example.com/repo.git")}}})
        override = _bottle(**{"git-gate": {"repos": {"myrepo": _git_repo("ssh://git@override.example.com/repo.git")}}})
        result = merge_bottles_runtime([base, override])
        self.assertEqual(1, len(result.git))
        self.assertEqual("ssh://git@override.example.com/repo.git", result.git[0].Upstream)

    def test_empty_list_raises(self):
        with self.assertRaises(ValueError):
            merge_bottles_runtime([])


class TestLoadForAgentWithBottleNames(unittest.TestCase):
    def test_bottle_names_pick_the_bottle(self):
        idx = _index(
            bottles={
                "base": {"env": {"X": "base"}},
                "override": {"env": {"X": "override"}},
            },
            agents={"impl": {"skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ("override",))
        self.assertEqual("override", m.bottle.env["X"])

    def test_bottle_names_merged_in_order(self):
        idx = _index(
            bottles={
                "a": {"env": {"X": "a", "A": "only-a"}},
                "b": {"env": {"X": "b", "B": "only-b"}},
            },
            agents={"impl": {"skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ("a", "b"))
        self.assertEqual("b", m.bottle.env["X"])
        self.assertEqual("only-a", m.bottle.env["A"])
        self.assertEqual("only-b", m.bottle.env["B"])

    def test_no_bottle_names_runs_in_the_built_in_basic(self):
        # Agents don't name a bottle (ADR 0011); none picked means `basic`.
        idx = _index(
            bottles={"base": {"env": {"X": "base"}}},
            agents={"impl": {"skills": [], "prompt": ""}},
        )
        m = idx.load_for_agent("impl", ())
        self.assertNotIn("X", m.bottle.env)
        self.assertIn("pypi.org", {r.Host for r in m.bottle.egress.routes})

    def test_a_basic_of_your_own_shadows_the_built_in(self):
        idx = _index(
            bottles={"basic": {"env": {"X": "mine"}}},
            agents={"impl": {"skills": [], "prompt": ""}},
        )
        self.assertEqual("mine", idx.load_for_agent("impl").bottle.env["X"])

    def test_unknown_bottle_name_raises(self):
        idx = _index(
            bottles={"base": {}},
            agents={"impl": {"skills": [], "prompt": ""}},
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
            agents={"impl": {"skills": [], "prompt": ""}},
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
            (agents_dir / "impl.md").write_text("---\n---\n")
            idx = ManifestIndex.resolve(str(home))
            self.assertEqual(["basic", "claude", "dev"], idx.all_bottle_names)  # basic is built in
        finally:
            if orig_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = orig_home
            shutil.rmtree(home, ignore_errors=True)


class TestAgentWithoutBottleMd(unittest.TestCase):
    """Agent files name no bottle: launches pick them, else `basic`."""

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

    def test_no_bottle_names_runs_in_basic(self):
        self._write("bottles/dev.md", "---\nenv:\n  X: dev\n---\n")
        self._write("agents/impl.md", "---\n---\nimpl agent.\n")
        idx = ManifestIndex.resolve(str(self.home))
        m = idx.load_for_agent("impl", ())
        self.assertNotIn("X", m.bottle.env)
        self.assertIn("pypi.org", {r.Host for r in m.bottle.egress.routes})

    def test_an_agent_naming_a_bottle_fails_saying_what_to_do(self):
        self._write("bottles/dev.md", "---\n---\n")
        self._write("agents/impl.md", "---\nbottle: dev\n---\nimpl agent.\n")
        idx = ManifestIndex.resolve(str(self.home))
        with self.assertRaises(ManifestError) as ctx:
            idx.load_for_agent("impl", ("dev",))
        self.assertIn("agents no longer name a bottle", str(ctx.exception))
        self.assertIn("`--bottle <name>`", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
