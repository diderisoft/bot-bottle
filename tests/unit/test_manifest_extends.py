"""Unit: bottle composition via `extends:` (PRD 0025, issue #88).

Each merge rule from the PRD gets a focused case; the resolver's
recursion + cycle / missing-parent / self-reference dies are in
their own tests.

The `Manifest.from_json_obj` path is the test surface — same
resolver runs from `Manifest.from_md_dirs` (md loader) so locking
it here covers both."""

from __future__ import annotations

import unittest

from bot_bottle.manifest import ManifestError, ManifestIndex


def _error_message(callable_, *args, **kwargs) -> str:  # type: ignore
    """Run `callable_` expecting a ManifestError; return its message."""
    try:
        callable_(*args, **kwargs)
    except ManifestError as e:
        return str(e)
    raise AssertionError("expected ManifestError was not raised")


def _build(**bottles) -> Manifest:  # type: ignore
    """Build a manifest with the given bottles and one trivial agent
    referencing the first bottle (so the manifest is valid)."""
    first = next(iter(bottles))
    return ManifestIndex.from_json_obj({
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


class TestExtendsGitMerge(unittest.TestCase):
    """git-gate.user overlays by field; git-gate.repos merges by name,
    with same-name child entries merging field-by-field (child wins)."""

    _GIT_ENTRY_A = {"url": "ssh://git@host-a/a.git", "key": {"provider": "static", "path": "/dev/null"}}
    _GIT_ENTRY_B = {"url": "ssh://git@host-b/b.git", "key": {"provider": "static", "path": "/dev/null"}}

    def test_child_git_repos_merge_with_parent(self):
        m = _build(
            base={"git-gate": {"repos": {"a": self._GIT_ENTRY_A}}},
            child={
                "extends": "base",
                "git-gate": {"repos": {"b": self._GIT_ENTRY_B}},
            },
        )
        names = [e.Name for e in m.bottles["child"].git]
        self.assertEqual(["a", "b"], names)

    def test_child_git_repo_different_name_same_host_coexists(self):
        # Repos are keyed by Name, not UpstreamHost: two repos with
        # different names on the same host both survive the merge.
        same_host_b = {"url": "ssh://git@host-a/b.git", "key": {"provider": "static", "path": "/dev/null"}}
        m = _build(
            base={"git-gate": {"repos": {"a": self._GIT_ENTRY_A}}},
            child={
                "extends": "base",
                "git-gate": {"repos": {"a2": same_host_b}},
            },
        )
        entries = m.bottles["child"].git
        self.assertEqual(2, len(entries))
        names = {e.Name for e in entries}
        self.assertEqual({"a", "a2"}, names)

    def test_child_omits_git_gate_inherits_full_list(self):
        m = _build(
            base={"git-gate": {"repos": {
                "a": self._GIT_ENTRY_A,
                "b": self._GIT_ENTRY_B,
            }}},
            child={"extends": "base"},
        )
        names = [e.Name for e in m.bottles["child"].git]
        self.assertEqual(["a", "b"], names)

    def test_child_explicit_empty_repos_clears_parent(self):
        # `git-gate.repos: {}` is the documented way to say "drop
        # the parent's repos" rather than "inherit them".
        m = _build(
            base={"git-gate": {"repos": {"a": self._GIT_ENTRY_A}}},
            child={"extends": "base", "git-gate": {"repos": {}}},
        )
        self.assertEqual((), m.bottles["child"].git)

    def test_child_same_name_repo_merges_key_field(self):
        # Issue #237: child repo with same name as parent should merge
        # field-by-field. Child overrides only `key`; parent's url and
        # host_key are preserved.
        parent_entry = {
            "url": "ssh://git@host-a/repo.git",
            "host_key": "ecdsa-sha2-nistp256 AAAA",
            "key": {"provider": "static", "path": "/keys/id_rsa"},
        }
        m = _build(
            base={"git-gate": {"repos": {"repo": parent_entry}}},
            child={
                "extends": "base",
                "git-gate": {"repos": {"repo": {
                    "key": {"provider": "gitea", "forge_token_env": "GITEA_TOKEN"},
                }}},
            },
        )
        entries = m.bottles["child"].git
        self.assertEqual(1, len(entries))
        e = entries[0]
        self.assertEqual("repo", e.Name)
        self.assertEqual("ssh://git@host-a/repo.git", e.Upstream)
        self.assertEqual("ecdsa-sha2-nistp256 AAAA", e.KnownHostKey)
        self.assertEqual("gitea", e.Key.provider)
        self.assertEqual("GITEA_TOKEN", e.Key.forge_token_env)

    def test_child_same_name_repo_overrides_url(self):
        # Child can override url on a same-name repo; other parent fields
        # fall through.
        parent_entry = {
            "url": "ssh://git@host-a/old.git",
            "key": {"provider": "static", "path": "/keys/id_rsa"},
        }
        m = _build(
            base={"git-gate": {"repos": {"repo": parent_entry}}},
            child={
                "extends": "base",
                "git-gate": {"repos": {"repo": {
                    "url": "ssh://git@host-b/new.git",
                    "key": {"provider": "static", "path": "/keys/id_rsa"},
                }}},
            },
        )
        entries = m.bottles["child"].git
        self.assertEqual(1, len(entries))
        self.assertEqual("ssh://git@host-b/new.git", entries[0].Upstream)

    def test_child_same_name_plus_new_repo(self):
        # Same-name repo is field-merged; a distinct new name in child
        # is appended.
        parent_entry = {
            "url": "ssh://git@host-a/repo.git",
            "key": {"provider": "static", "path": "/keys/id_rsa"},
        }
        m = _build(
            base={"git-gate": {"repos": {"repo": parent_entry}}},
            child={
                "extends": "base",
                "git-gate": {"repos": {
                    "repo": {"key": {"provider": "gitea", "forge_token_env": "TOK"}},
                    "other": self._GIT_ENTRY_B,
                }},
            },
        )
        child = m.bottles["child"]
        names = {e.Name for e in child.git}
        self.assertEqual({"repo", "other"}, names)
        repo_entry = next(e for e in child.git if e.Name == "repo")
        self.assertEqual("gitea", repo_entry.Key.provider)

    def test_child_git_user_inherits_parent_repos(self):
        m = _build(
            base={"git-gate": {"repos": {"a": self._GIT_ENTRY_A}}},
            child={"extends": "base", "git-gate": {"user": {"name": "Child"}}},
        )
        self.assertEqual(["a"], [e.Name for e in m.bottles["child"].git])
        self.assertEqual("Child", m.bottles["child"].git_user.name)


class TestExtendsEgressMerge(unittest.TestCase):
    """egress.routes merges; egress.log overlays only when declared."""

    def test_child_egress_routes_merge_with_parent(self):
        m = _build(
            base={"egress": {"routes": [{"host": "a.example.com"}]}},
            child={
                "extends": "base",
                "egress": {"routes": [{"host": "b.example.com"}]},
            },
        )
        hosts = [r.Host for r in m.bottles["child"].egress.routes]
        self.assertEqual(["a.example.com", "b.example.com"], hosts)

    def test_child_omits_egress_inherits(self):
        m = _build(
            base={"egress": {"routes": [{"host": "a.example.com"}]}},
            child={"extends": "base"},
        )
        hosts = [r.Host for r in m.bottles["child"].egress.routes]
        self.assertEqual(["a.example.com"], hosts)

    def test_child_egress_log_inherits_parent_routes(self):
        m = _build(
            base={
                "egress": {
                    "routes": [{"host": "a.example.com"}],
                    "log": 1,
                },
            },
            child={"extends": "base", "egress": {"log": 2}},
        )
        child = m.bottles["child"].egress
        self.assertEqual(["a.example.com"], [r.Host for r in child.routes])
        self.assertEqual(2, child.Log)

    def test_child_egress_routes_inherit_parent_log_when_omitted(self):
        m = _build(
            base={
                "egress": {
                    "routes": [{"host": "a.example.com"}],
                    "log": 1,
                },
            },
            child={
                "extends": "base",
                "egress": {"routes": [{"host": "b.example.com"}]},
            },
        )
        child = m.bottles["child"].egress
        self.assertEqual(
            ["a.example.com", "b.example.com"],
            [r.Host for r in child.routes],
        )
        self.assertEqual(1, child.Log)

    def test_duplicate_host_across_parent_and_child_dies(self):
        msg = _error_message(
            _build,
            base={"egress": {"routes": [{"host": "a.example.com"}]}},
            child={
                "extends": "base",
                "egress": {"routes": [{"host": "A.EXAMPLE.COM"}]},
            },
        )
        self.assertIn("duplicate host", msg)
        self.assertIn("A.EXAMPLE.COM", msg)


class TestExtendsGitUserOverlay(unittest.TestCase):
    """git-gate.user: per-field overlay. Each non-empty field on child
    wins; empties fall through to parent."""

    def test_parent_full_child_omits(self):
        m = _build(
            base={"git-gate": {"user": {"name": "Parent", "email": "p@x"}}},
            child={"extends": "base"},
        )
        u = m.bottles["child"].git_user
        self.assertEqual("Parent", u.name)
        self.assertEqual("p@x", u.email)

    def test_child_overrides_both(self):
        m = _build(
            base={"git-gate": {"user": {"name": "Parent", "email": "p@x"}}},
            child={
                "extends": "base",
                "git-gate": {"user": {"name": "Child", "email": "c@x"}},
            },
        )
        u = m.bottles["child"].git_user
        self.assertEqual("Child", u.name)
        self.assertEqual("c@x", u.email)

    def test_child_adds_email_inherits_name(self):
        m = _build(
            base={"git-gate": {"user": {"name": "Parent"}}},
            child={"extends": "base", "git-gate": {"user": {"email": "c@x"}}},
        )
        u = m.bottles["child"].git_user
        self.assertEqual("Parent", u.name)
        self.assertEqual("c@x", u.email)

    def test_child_overrides_only_email(self):
        m = _build(
            base={"git-gate": {"user": {"name": "Parent", "email": "p@x"}}},
            child={"extends": "base", "git-gate": {"user": {"email": "c@x"}}},
        )
        u = m.bottles["child"].git_user
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
        msg = _error_message(_build, child={"extends": "ghost"})
        self.assertIn("extends 'ghost'", msg)
        self.assertIn("not defined", msg)

    def test_self_extends_dies(self):
        msg = _error_message(_build, loop={"extends": "loop"})
        self.assertIn("extends itself", msg)

    def test_two_node_cycle_dies(self):
        msg = _error_message(
            _build,
            a={"extends": "b"},
            b={"extends": "a"},
        )
        self.assertIn("extends cycle", msg)
        # Chain should include both names.
        self.assertIn("a", msg)
        self.assertIn("b", msg)

    def test_three_node_cycle_dies(self):
        msg = _error_message(
            _build,
            a={"extends": "b"},
            b={"extends": "c"},
            c={"extends": "a"},
        )
        self.assertIn("extends cycle", msg)

    def test_non_string_extends_dies(self):
        msg = _error_message(_build, child={"extends": ["base"]})
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
