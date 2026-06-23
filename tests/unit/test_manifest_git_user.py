"""Unit: Bottle git-gate.user manifest parsing + validation (issue #86, PRD 0047)."""

import unittest

from bot_bottle.manifest import ManifestError, ManifestGitUser, ManifestIndex


def _error_message(callable_, *args, **kwargs) -> str:  # type: ignore
    """Run `callable_` expecting a ManifestError; return its message."""
    try:
        callable_(*args, **kwargs)
    except ManifestError as e:
        return str(e)
    raise AssertionError("expected ManifestError was not raised")


def _manifest(git_user):  # type: ignore
    return {
        "bottles": {"dev": {"git-gate": {"user": git_user}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestGitUserParsing(unittest.TestCase):
    def test_parses_both_fields(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "name": "Eric Bauerfeld",
            "email": "eric+claude@dideric.is",
        }))
        u = m.bottles["dev"].git_user
        self.assertEqual("Eric Bauerfeld", u.name)
        self.assertEqual("eric+claude@dideric.is", u.email)
        self.assertFalse(u.is_empty())

    def test_name_only(self):
        m = ManifestIndex.from_json_obj(_manifest({"name": "Bot"}))
        u = m.bottles["dev"].git_user
        self.assertEqual("Bot", u.name)
        self.assertEqual("", u.email)

    def test_email_only(self):
        m = ManifestIndex.from_json_obj(_manifest({"email": "bot@example.com"}))
        u = m.bottles["dev"].git_user
        self.assertEqual("", u.name)
        self.assertEqual("bot@example.com", u.email)

    def test_omitted_defaults_to_empty(self):
        # No git.user block at all → empty GitUser, is_empty True →
        # provisioner skips the `git config` step entirely.
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        u = m.bottles["dev"].git_user
        self.assertTrue(u.is_empty())

    def test_both_empty_strings_dies(self):
        # An explicit `git.user: {name: "", email: ""}` is a typo
        # /  half-finished edit; fail loudly rather than silently
        # no-op (the operator clearly meant to configure something).
        msg = _error_message(
            ManifestIndex.from_json_obj, _manifest({"name": "", "email": ""}),
        )
        self.assertIn("neither name nor email", msg)

    def test_unknown_key_dies(self):
        msg = _error_message(
            ManifestIndex.from_json_obj,
            _manifest({"name": "Bot", "username": "bot"}),
        )
        self.assertIn("unknown key", msg)
        self.assertIn("username", msg)

    def test_non_string_name_dies(self):
        msg = _error_message(
            ManifestIndex.from_json_obj, _manifest({"name": 42}),
        )
        self.assertIn("git-gate.user.name must be a string", msg)

    def test_non_string_email_dies(self):
        msg = _error_message(
            ManifestIndex.from_json_obj, _manifest({"email": ["x@y.z"]}),
        )
        self.assertIn("git-gate.user.email must be a string", msg)

    def test_legacy_top_level_git_user_dies(self):
        msg = _error_message(
            ManifestIndex.from_json_obj,
            {
                "bottles": {"dev": {"git_user": {"name": "Bot"}}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            },
        )
        self.assertIn("git_user", msg)
        self.assertIn("git-gate.user", msg)


class TestGitUserDirect(unittest.TestCase):
    """Direct GitUser dataclass exercises (no manifest wrapper)."""

    def test_is_empty_default(self):
        self.assertTrue(ManifestGitUser().is_empty())

    def test_is_empty_false_when_name_set(self):
        self.assertFalse(ManifestGitUser(name="x").is_empty())

    def test_is_empty_false_when_email_set(self):
        self.assertFalse(ManifestGitUser(email="x@y").is_empty())


if __name__ == "__main__":
    unittest.main()
