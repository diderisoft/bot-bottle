"""Unit: git-gate.repos manifest parsing + validation (PRD 0047)."""

import unittest

from bot_bottle.manifest import ManifestError, Manifest


def _manifest(repos: dict) -> dict:
    return {
        "bottles": {"dev": {"git-gate": {"repos": repos}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestGitEntryParsing(unittest.TestCase):
    def test_parses_minimal_entry(self):
        m = Manifest.from_json_obj(_manifest({
            "bot-bottle": {
                "url": "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
                "identity": "/dev/null",
            },
        }))
        entries = m.bottles["dev"].git
        self.assertEqual(1, len(entries))
        e = entries[0]
        self.assertEqual("bot-bottle", e.Name)
        self.assertEqual("git", e.UpstreamUser)
        self.assertEqual("gitea.dideric.is", e.UpstreamHost)
        self.assertEqual("30009", e.UpstreamPort)
        self.assertEqual("didericis/bot-bottle.git", e.UpstreamPath)

    def test_default_port_is_22(self):
        m = Manifest.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/didericis/foo.git",
                "identity": "/dev/null",
            },
        }))
        e = m.bottles["dev"].git[0]
        self.assertEqual("22", e.UpstreamPort)
        self.assertEqual("github.com", e.UpstreamHost)

    def test_host_key_optional(self):
        m = Manifest.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/foo.git",
                "identity": "/dev/null",
            },
        }))
        self.assertEqual("", m.bottles["dev"].git[0].KnownHostKey)

    def test_host_key_stored(self):
        m = Manifest.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/foo.git",
                "identity": "/dev/null",
                "host_key": "ssh-ed25519 AAAA",
            },
        }))
        self.assertEqual("ssh-ed25519 AAAA", m.bottles["dev"].git[0].KnownHostKey)

    def test_repo_name_becomes_Name(self):
        m = Manifest.from_json_obj(_manifest({
            "my-repo": {
                "url": "ssh://git@github.com/foo.git",
                "identity": "/dev/null",
            },
        }))
        self.assertEqual("my-repo", m.bottles["dev"].git[0].Name)

    def test_missing_url_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {"identity": "/dev/null"},
            }))

    def test_missing_identity_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {"url": "ssh://git@github.com/foo.git"},
            }))

    def test_unknown_key_in_entry_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "identity": "/dev/null",
                    "IdentityFile": "/dev/null",  # old PascalCase key
                },
            }))

    def test_non_ssh_url_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {
                    "url": "https://github.com/didericis/foo.git",
                    "identity": "/dev/null",
                },
            }))

    def test_scp_style_url_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {
                    "url": "git@github.com:didericis/foo.git",
                    "identity": "/dev/null",
                },
            }))

    def test_url_without_user_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://github.com/foo.git",
                    "identity": "/dev/null",
                },
            }))

    def test_url_without_path_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com",
                    "identity": "/dev/null",
                },
            }))

    def test_non_numeric_port_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com:notaport/foo.git",
                    "identity": "/dev/null",
                },
            }))

    def test_ip_literal_upstream(self):
        m = Manifest.from_json_obj(_manifest({
            "bot-bottle": {
                "url": "ssh://git@192.0.2.10:30009/didericis/bot-bottle.git",
                "identity": "/dev/null",
            },
        }))
        e = m.bottles["dev"].git[0]
        self.assertEqual("192.0.2.10", e.UpstreamHost)
        self.assertEqual("30009", e.UpstreamPort)
        self.assertEqual("bot-bottle", e.Name)


class TestGitEntryCrossValidation(unittest.TestCase):
    def test_two_repos_different_hosts_both_parsed(self):
        # Repo names come from dict keys; two distinct keys always produce
        # two distinct entries (uniqueness is guaranteed at the YAML/dict level).
        m = Manifest.from_json_obj({
            "bottles": {"dev": {"git-gate": {"repos": {
                "foo": {
                    "url": "ssh://git@a.example/x.git",
                    "identity": "/dev/null",
                },
                "bar": {
                    "url": "ssh://git@b.example/y.git",
                    "identity": "/dev/null",
                },
            }}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        names = {e.Name for e in m.bottles["dev"].git}
        self.assertEqual({"foo", "bar"}, names)

    def test_legacy_ssh_field_dies_with_hint(self):
        with self.assertRaises(ManifestError):
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
                    },
                },
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_legacy_git_key_dies_with_hint(self):
        msg = ""
        try:
            Manifest.from_json_obj({
                "bottles": {"dev": {"git": {"remotes": {}}}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })
        except ManifestError as e:
            msg = str(e)
        self.assertIn("git-gate", msg)
        self.assertIn("PRD 0047", msg)


class TestEmptyGitGateField(unittest.TestCase):
    def test_no_git_gate_field_yields_empty_tuple(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)

    def test_git_gate_object_type_required(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"git-gate": "not-a-dict"}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_empty_repos_yields_empty_tuple(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {"git-gate": {"repos": {}}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)


if __name__ == "__main__":
    unittest.main()
