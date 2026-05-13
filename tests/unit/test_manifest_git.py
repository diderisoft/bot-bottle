"""Unit: Bottle.git manifest parsing + validation (PRD 0008)."""

import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Manifest


def _manifest(git_entries):
    return {
        "bottles": {"dev": {"git": git_entries}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestGitEntryParsing(unittest.TestCase):
    def test_parses_minimal_entry(self):
        m = Manifest.from_json_obj(_manifest([{
            "Name": "claude-bottle",
            "Upstream": "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
            "IdentityFile": "/dev/null",
        }]))
        entries = m.bottles["dev"].git
        self.assertEqual(1, len(entries))
        e = entries[0]
        self.assertEqual("claude-bottle", e.Name)
        self.assertEqual("git", e.UpstreamUser)
        self.assertEqual("gitea.dideric.is", e.UpstreamHost)
        self.assertEqual("30009", e.UpstreamPort)
        self.assertEqual("didericis/claude-bottle.git", e.UpstreamPath)

    def test_default_port_is_22(self):
        m = Manifest.from_json_obj(_manifest([{
            "Name": "foo",
            "Upstream": "ssh://git@github.com/didericis/foo.git",
            "IdentityFile": "/dev/null",
        }]))
        e = m.bottles["dev"].git[0]
        self.assertEqual("22", e.UpstreamPort)
        self.assertEqual("github.com", e.UpstreamHost)

    def test_known_host_key_optional(self):
        m = Manifest.from_json_obj(_manifest([{
            "Name": "foo",
            "Upstream": "ssh://git@github.com/foo.git",
            "IdentityFile": "/dev/null",
        }]))
        self.assertEqual("", m.bottles["dev"].git[0].KnownHostKey)

    def test_missing_name_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Upstream": "ssh://git@github.com/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_missing_upstream_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "IdentityFile": "/dev/null",
            }]))

    def test_missing_identity_file_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://git@github.com/foo.git",
            }]))

    def test_non_ssh_upstream_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "https://github.com/didericis/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_scp_style_upstream_dies(self):
        # SCP-style "git@host:path" is intentionally not supported in
        # v1 — ssh:// only.
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "git@github.com:didericis/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_upstream_without_user_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://github.com/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_upstream_without_path_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://git@github.com",
                "IdentityFile": "/dev/null",
            }]))

    def test_non_numeric_port_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://git@github.com:notaport/foo.git",
                "IdentityFile": "/dev/null",
            }]))


class TestGitEntryCrossValidation(unittest.TestCase):
    def test_duplicate_name_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Name": "foo", "Upstream": "ssh://git@a.example/x.git",
                 "IdentityFile": "/dev/null"},
                {"Name": "foo", "Upstream": "ssh://git@b.example/y.git",
                 "IdentityFile": "/dev/null"},
            ]))

    def test_shadow_route_with_ssh_entry_dies(self):
        # An ssh entry pointing at gitea.dideric.is:30009 AND a git
        # entry pointing at ssh://git@gitea.dideric.is:30009/... is a
        # bypass: agents could route around the gate by using the
        # ssh-gate. Manifest construction must reject.
        with self.assertRaises(Die):
            Manifest.from_json_obj({
                "bottles": {
                    "dev": {
                        "ssh": [{
                            "Host": "gitea",
                            "IdentityFile": "/dev/null",
                            "Hostname": "gitea.dideric.is",
                            "User": "git",
                            "Port": 30009,
                        }],
                        "git": [{
                            "Name": "claude-bottle",
                            "Upstream": "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
                            "IdentityFile": "/dev/null",
                        }],
                    },
                },
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_independent_ssh_and_git_targets_allowed(self):
        # Same hostname but different ports are independent targets.
        m = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "ssh": [{
                        "Host": "gitea-ssh",
                        "IdentityFile": "/dev/null",
                        "Hostname": "gitea.dideric.is",
                        "User": "git",
                        "Port": 22,
                    }],
                    "git": [{
                        "Name": "claude-bottle",
                        "Upstream": "ssh://git@gitea.dideric.is:30009/didericis/claude-bottle.git",
                        "IdentityFile": "/dev/null",
                    }],
                },
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual(1, len(m.bottles["dev"].ssh))
        self.assertEqual(1, len(m.bottles["dev"].git))


class TestEmptyGitField(unittest.TestCase):
    def test_no_git_field_yields_empty_tuple(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)

    def test_git_array_type_required(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj({
                "bottles": {"dev": {"git": "not-a-list"}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
