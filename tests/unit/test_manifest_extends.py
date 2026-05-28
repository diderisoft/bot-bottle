"""Unit: bottle composition via `extends:` (PRD 0025, issue #88).

Each merge rule from the PRD gets a focused case; the resolver's
recursion + cycle / missing-parent / self-reference dies are in
their own tests.

The `Manifest.from_json_obj` path is the test surface — same
resolver runs from `Manifest.from_md_dirs` (md loader) so locking
it here covers both."""

from __future__ import annotations

import contextlib
import io
import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Manifest


def _die_message(callable_, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        try:
            callable_(*args, **kwargs)
        except Die:
            return buf.getvalue()
    raise AssertionError("expected Die was not raised")


def _build(**bottles) -> Manifest:
    """Build a manifest with the given bottles and one trivial agent
    referencing the first bottle (so the manifest is valid)."""
    first = next(iter(bottles))
    return Manifest.from_json_obj({
        "bottles": bottles,
        "agents": {
            "demo": {"skills": [], "prompt": "", "bottle": first},
        },
    })


class TestExtendsBasic(unittest.TestCase):
    def test_leaf_without_extends_unchanged(self):
        # Sanity: existing manifests with no `extends:` parse the
        # same way they did before the resolver landed.
        m = _build(dev={
            "env": {"FOO": "bar"},
            "supervise": True,
        })
        b = m.bottles["dev"]
        self.assertEqual({"FOO": "bar"}, dict(b.env))
        self.assertTrue(b.supervise)

    def test_child_inherits_parent_fields_unchanged(self):
        m = _build(
            base={
                "env": {"BASE": "1"},
                "supervise": True,
            },
            child={"extends": "base"},
        )
        c = m.bottles["child"]
        self.assertEqual({"BASE": "1"}, dict(c.env))
        self.assertTrue(c.supervise)

    def test_child_overrides_supervise_scalar(self):
        m = _build(
            base={"supervise": True},
            off={"extends": "base", "supervise": False},
        )
        self.assertTrue(m.bottles["base"].supervise)
        self.assertFalse(m.bottles["off"].supervise)

    def test_parent_resolved_once_for_multiple_children(self):
        # Two children sharing one parent: both inherit; the parent
        # is resolved once + cached. (Cache behavior is internal; we
        # observe correctness on both children.)
        m = _build(
            base={"env": {"BASE": "1"}, "supervise": True},
            a={"extends": "base", "env": {"A": "1"}},
            b={"extends": "base", "env": {"B": "1"}},
        )
        self.assertEqual({"BASE": "1", "A": "1"}, dict(m.bottles["a"].env))
        self.assertEqual({"BASE": "1", "B": "1"}, dict(m.bottles["b"].env))


class TestExtendsEnvMerge(unittest.TestCase):
    """env: dict merge, child wins on key collision."""

    def test_disjoint_keys_union(self):
        m = _build(
            base={"env": {"PARENT_ONLY": "p"}},
            child={"extends": "base", "env": {"CHILD_ONLY": "c"}},
        )
        self.assertEqual(
            {"PARENT_ONLY": "p", "CHILD_ONLY": "c"},
            dict(m.bottles["child"].env),
        )

    def test_collision_child_wins(self):
        m = _build(
            base={"env": {"SHARED": "from-parent"}},
            child={"extends": "base", "env": {"SHARED": "from-child"}},
        )
        self.assertEqual(
            {"SHARED": "from-child"},
            dict(m.bottles["child"].env),
        )

    def test_child_omits_env_inherits_full(self):
        m = _build(
            base={"env": {"A": "1", "B": "2"}},
            child={"extends": "base"},
        )
        self.assertEqual({"A": "1", "B": "2"}, dict(m.bottles["child"].env))


class TestExtendsListsFullReplace(unittest.TestCase):
    """git: and egress: are full-replace when the child declares
    them — partial merge would be ambiguous (ordering + name
    collisions). See PRD 0025 "Merge rules"."""

    _GIT_ENTRY_A = {
        "Name": "a",
        "Upstream": "ssh://git@host-a/a.git",
        "IdentityFile": "/dev/null",
    }
    _GIT_ENTRY_B = {
        "Name": "b",
        "Upstream": "ssh://git@host-b/b.git",
        "IdentityFile": "/dev/null",
    }

    def test_child_git_replaces_parent_entirely(self):
        m = _build(
            base={"git": [self._GIT_ENTRY_A]},
            child={"extends": "base", "git": [self._GIT_ENTRY_B]},
        )
        names = [e.Name for e in m.bottles["child"].git]
        self.assertEqual(["b"], names)

    def test_child_omits_git_inherits_full_list(self):
        m = _build(
            base={"git": [self._GIT_ENTRY_A, self._GIT_ENTRY_B]},
            child={"extends": "base"},
        )
        names = [e.Name for e in m.bottles["child"].git]
        self.assertEqual(["a", "b"], names)

    def test_child_explicit_empty_git_clears_parent(self):
        # `git: []` is the documented way to say "drop the
        # parent's list" rather than "inherit it".
        m = _build(
            base={"git": [self._GIT_ENTRY_A]},
            child={"extends": "base", "git": []},
        )
        self.assertEqual((), m.bottles["child"].git)

    def test_child_egress_replaces_parent_entirely(self):
        m = _build(
            base={"egress": {"routes": [{"host": "a.example.com"}]}},
            child={
                "extends": "base",
                "egress": {"routes": [{"host": "b.example.com"}]},
            },
        )
        hosts = [r.Host for r in m.bottles["child"].egress.routes]
        self.assertEqual(["b.example.com"], hosts)

    def test_child_omits_egress_inherits(self):
        m = _build(
            base={"egress": {"routes": [{"host": "a.example.com"}]}},
            child={"extends": "base"},
        )
        hosts = [r.Host for r in m.bottles["child"].egress.routes]
        self.assertEqual(["a.example.com"], hosts)


class TestExtendsGitUserOverlay(unittest.TestCase):
    """git_user: per-field overlay. Each non-empty field on child
    wins; empties fall through to parent."""

    def test_parent_full_child_omits(self):
        m = _build(
            base={"git_user": {"name": "Parent", "email": "p@x"}},
            child={"extends": "base"},
        )
        u = m.bottles["child"].git_user
        self.assertEqual("Parent", u.name)
        self.assertEqual("p@x", u.email)

    def test_child_overrides_both(self):
        m = _build(
            base={"git_user": {"name": "Parent", "email": "p@x"}},
            child={
                "extends": "base",
                "git_user": {"name": "Child", "email": "c@x"},
            },
        )
        u = m.bottles["child"].git_user
        self.assertEqual("Child", u.name)
        self.assertEqual("c@x", u.email)

    def test_child_adds_email_inherits_name(self):
        # Parent sets only name; child sets only email. Both end
        # up populated on the child.
        m = _build(
            base={"git_user": {"name": "Parent"}},
            child={"extends": "base", "git_user": {"email": "c@x"}},
        )
        u = m.bottles["child"].git_user
        self.assertEqual("Parent", u.name)
        self.assertEqual("c@x", u.email)

    def test_child_overrides_only_email(self):
        m = _build(
            base={"git_user": {"name": "Parent", "email": "p@x"}},
            child={"extends": "base", "git_user": {"email": "c@x"}},
        )
        u = m.bottles["child"].git_user
        # Child overrides email; name inherited from parent.
        self.assertEqual("Parent", u.name)
        self.assertEqual("c@x", u.email)


class TestExtendsChain(unittest.TestCase):
    """Multi-step inheritance: A extends B extends C."""

    def test_three_step_chain(self):
        m = _build(
            grandparent={
                "env": {"GP": "1"},
                "supervise": True,
            },
            parent={
                "extends": "grandparent",
                "env": {"P": "1"},
            },
            child={
                "extends": "parent",
                "env": {"C": "1"},
            },
        )
        self.assertEqual(
            {"GP": "1", "P": "1", "C": "1"},
            dict(m.bottles["child"].env),
        )
        # supervise threads through unchanged.
        self.assertTrue(m.bottles["child"].supervise)

    def test_intermediate_can_override(self):
        m = _build(
            grandparent={"env": {"X": "from-gp"}},
            parent={"extends": "grandparent", "env": {"X": "from-p"}},
            child={"extends": "parent"},
        )
        self.assertEqual("from-p", m.bottles["child"].env["X"])


class TestExtendsErrors(unittest.TestCase):
    def test_missing_parent_dies(self):
        msg = _die_message(_build, child={"extends": "ghost"})
        self.assertIn("extends 'ghost'", msg)
        self.assertIn("not defined", msg)

    def test_self_extends_dies(self):
        msg = _die_message(_build, loop={"extends": "loop"})
        self.assertIn("extends itself", msg)

    def test_two_node_cycle_dies(self):
        msg = _die_message(
            _build,
            a={"extends": "b"},
            b={"extends": "a"},
        )
        self.assertIn("extends cycle", msg)
        # Chain should include both names.
        self.assertIn("a", msg)
        self.assertIn("b", msg)

    def test_three_node_cycle_dies(self):
        msg = _die_message(
            _build,
            a={"extends": "b"},
            b={"extends": "c"},
            c={"extends": "a"},
        )
        self.assertIn("extends cycle", msg)

    def test_non_string_extends_dies(self):
        msg = _die_message(_build, child={"extends": ["base"]})
        self.assertIn("extends must be a string", msg)


class TestExtendsAvailableInBottleKeys(unittest.TestCase):
    """`extends` must not trip the unknown-keys check in the md
    loader. Verified indirectly via from_json_obj (same resolver)
    + a positive parse here."""

    def test_extends_alone_parses(self):
        # No other fields; child purely inherits.
        m = _build(
            base={"env": {"A": "1"}},
            child={"extends": "base"},
        )
        self.assertEqual({"A": "1"}, dict(m.bottles["child"].env))


if __name__ == "__main__":
    unittest.main()
