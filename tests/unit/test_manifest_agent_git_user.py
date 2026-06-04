"""Unit: agent-level git-gate.user overlay + provenance (PRD 0027, PRD 0047).

An agent file may declare `git-gate.user` (name/email). At
`Manifest.bottle_for()` it overlays the referenced bottle's
`git-gate.user` per-field, agent-wins-on-non-empty. `git-gate.repos` is
rejected on agents. `Manifest.git_identity_summary()` reports the
effective identity with per-field `(agent)`/`(bottle)` provenance.

The `from_json_obj` path drives `Agent.from_dict` + `bottle_for`;
a temp-dir case locks the md loader (the `_AGENT_KEYS` allow + the
`git-gate` threading into `agent_dict`)."""

from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from bot_bottle.manifest import ManifestError, Manifest


def _error_message(callable_, *args, **kwargs) -> str:  # type: ignore
    """Run `callable_` expecting a ManifestError; return its message."""
    try:
        callable_(*args, **kwargs)
    except ManifestError as e:
        return str(e)
    raise AssertionError("expected ManifestError was not raised")


def _manifest(*, bottle_user=None, agent_git=None) -> Manifest:  # type: ignore
    bottle: dict = {}  # type: ignore
    if bottle_user is not None:
        bottle = {"git-gate": {"user": bottle_user}}
    agent: dict = {"skills": [], "prompt": "", "bottle": "dev"}  # type: ignore
    if agent_git is not None:
        agent["git-gate"] = agent_git
    return Manifest.from_json_obj({
        "bottles": {"dev": bottle},
        "agents": {"impl": agent},
    })


class TestAgentGitUserOverlay(unittest.TestCase):
    def test_agent_supplies_both_fields(self):
        m = _manifest(agent_git={"user": {"name": "a", "email": "a@b"}})
        u = m.bottle_for("impl").git_user
        self.assertEqual("a", u.name)
        self.assertEqual("a@b", u.email)

    def test_agent_name_only_email_falls_through_to_bottle(self):
        m = _manifest(
            bottle_user={"name": "B", "email": "b@c"},
            agent_git={"user": {"name": "a"}},
        )
        u = m.bottle_for("impl").git_user
        self.assertEqual("a", u.name)       # agent wins
        self.assertEqual("b@c", u.email)    # bottle falls through

    def test_agent_email_only_name_falls_through_to_bottle(self):
        m = _manifest(
            bottle_user={"name": "B", "email": "b@c"},
            agent_git={"user": {"email": "a@b"}},
        )
        u = m.bottle_for("impl").git_user
        self.assertEqual("B", u.name)
        self.assertEqual("a@b", u.email)

    def test_agent_identity_with_bottle_declaring_none(self):
        m = _manifest(agent_git={"user": {"name": "a", "email": "a@b"}})
        self.assertTrue(m.bottles["dev"].git_user.is_empty())
        self.assertFalse(m.bottle_for("impl").git_user.is_empty())

    def test_bottle_only_identity_preserved_when_agent_silent(self):
        m = _manifest(bottle_user={"name": "B", "email": "b@c"})
        u = m.bottle_for("impl").git_user
        self.assertEqual("B", u.name)
        self.assertEqual("b@c", u.email)

    def test_bottle_for_returns_same_instance_when_no_overlay(self):
        m = _manifest(bottle_user={"name": "B"})
        self.assertIs(m.bottles["dev"], m.bottle_for("impl"))

    def test_bottle_for_returns_same_instance_when_overlay_is_noop(self):
        m = _manifest(
            bottle_user={"name": "B", "email": "b@c"},
            agent_git={"user": {"name": "B", "email": "b@c"}},
        )
        self.assertIs(m.bottles["dev"], m.bottle_for("impl"))

    def test_other_bottle_fields_untouched_by_overlay(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {
                "env": {"FOO": "bar"},
                "supervise": True,
                "git-gate": {"user": {"name": "B"}},
            }},
            "agents": {"impl": {
                "bottle": "dev", "skills": [], "prompt": "",
                "git-gate": {"user": {"name": "a"}},
            }},
        })
        b = m.bottle_for("impl")
        self.assertEqual("a", b.git_user.name)
        self.assertEqual({"FOO": "bar"}, dict(b.env))
        self.assertTrue(b.supervise)


class TestAgentGitUserRejections(unittest.TestCase):
    def test_agent_repos_dies_bottle_only(self):
        msg = _error_message(_manifest, agent_git={
            "repos": {"r": {"url": "ssh://git@x/y.git", "identity": "/dev/null"}},
        })
        self.assertIn("git-gate.repos", msg)
        self.assertIn("bottle-only", msg)

    def test_agent_unknown_git_subkey_dies(self):
        msg = _error_message(_manifest, agent_git={"nope": {}})
        self.assertIn("not allowed at the agent level", msg)

    def test_agent_git_user_both_empty_dies(self):
        msg = _error_message(_manifest, agent_git={"user": {"name": "", "email": ""}})
        self.assertIn("neither name nor email", msg)


class TestGitIdentitySummary(unittest.TestCase):
    def test_both_from_agent(self):
        m = _manifest(agent_git={"user": {"name": "a", "email": "a@b"}})
        self.assertEqual(
            "name=a (agent), email=a@b (agent)",
            m.git_identity_summary("impl"),
        )

    def test_mixed_provenance(self):
        m = _manifest(
            bottle_user={"name": "B", "email": "b@c"},
            agent_git={"user": {"name": "a"}},
        )
        self.assertEqual(
            "name=a (agent), email=b@c (bottle)",
            m.git_identity_summary("impl"),
        )

    def test_bottle_only(self):
        m = _manifest(bottle_user={"name": "B", "email": "b@c"})
        self.assertEqual(
            "name=B (bottle), email=b@c (bottle)",
            m.git_identity_summary("impl"),
        )

    def test_none_when_unset_anywhere(self):
        m = _manifest()
        self.assertIsNone(m.git_identity_summary("impl"))


_BOTTLE_DEV = """
    ---
    git-gate:
      user:
        name: bottle-name
        email: bottle@example.com
    ---

    dev bottle.
"""

_AGENT_WITH_GIT = """
    ---
    bottle: dev
    git-gate:
      user:
        name: agent-name
    ---

    impl agent.
"""

_AGENT_WITH_REPOS = """
    ---
    bottle: dev
    git-gate:
      repos:
        r:
          url: ssh://git@x/y.git
          identity: /dev/null
    ---

    bad agent.
"""


class TestAgentGitUserMdLoader(unittest.TestCase):
    """Locks the md path: `git-gate` is an accepted agent key and threads
    into the parsed Agent (not rejected as an unknown frontmatter key),
    and agent `git-gate.repos` dies through the same loader."""

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

    def test_md_agent_git_user_overlays_bottle(self):
        self._write("bottles/dev.md", _BOTTLE_DEV)
        self._write("agents/impl.md", _AGENT_WITH_GIT)
        m = Manifest.resolve(str(self.home))
        u = m.bottle_for("impl").git_user
        self.assertEqual("agent-name", u.name)
        self.assertEqual("bottle@example.com", u.email)
        self.assertEqual(
            "name=agent-name (agent), email=bottle@example.com (bottle)",
            m.git_identity_summary("impl"),
        )

    def test_md_agent_repos_dies(self):
        self._write("bottles/dev.md", _BOTTLE_DEV)
        self._write("agents/impl.md", _AGENT_WITH_REPOS)
        msg = _error_message(Manifest.resolve, str(self.home))
        self.assertIn("git-gate.repos", msg)
        self.assertIn("bottle-only", msg)


if __name__ == "__main__":
    unittest.main()
