"""Unit: Bottle.git manifest parsing + validation (PRD 0008)."""

import unittest

from bot_bottle.manifest import ManifestError, Manifest


def _manifest(git_entries):
    return {
        "bottles": {"dev": {"git": {"remotes": {
            _host_for(entry): entry for entry in git_entries
        }}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


def _host_for(entry):
    upstream = entry.get("Upstream", "")
    if "@a.example" in upstream:
        return "a.example"
    if "@b.example" in upstream:
        return "b.example"
    if "@github.com" in upstream:
        return "github.com"
    if "@gitea.dideric.is" in upstream:
        return "gitea.dideric.is"
    return "example.com"


class TestGitEntryParsing(unittest.TestCase):
    def test_parses_minimal_entry(self):
        m = Manifest.from_json_obj(_manifest([{
            "Name": "bot-bottle",
            "Upstream": "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
            "IdentityFile": "/dev/null",
        }]))
        entries = m.bottles["dev"].git
        self.assertEqual(1, len(entries))
        e = entries[0]
        self.assertEqual("bot-bottle", e.Name)
        self.assertEqual("git", e.UpstreamUser)
        self.assertEqual("gitea.dideric.is", e.UpstreamHost)
        self.assertEqual("30009", e.UpstreamPort)
        self.assertEqual("didericis/bot-bottle.git", e.UpstreamPath)

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
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Upstream": "ssh://git@github.com/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_missing_upstream_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "IdentityFile": "/dev/null",
            }]))

    def test_missing_identity_file_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://git@github.com/foo.git",
            }]))

    def test_non_ssh_upstream_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "https://github.com/didericis/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_scp_style_upstream_dies(self):
        # SCP-style "git@host:path" is intentionally not supported in
        # v1 — ssh:// only.
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "git@github.com:didericis/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_upstream_without_user_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://github.com/foo.git",
                "IdentityFile": "/dev/null",
            }]))

    def test_upstream_without_path_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://git@github.com",
                "IdentityFile": "/dev/null",
            }]))

    def test_non_numeric_port_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj(_manifest([{
                "Name": "foo",
                "Upstream": "ssh://git@github.com:notaport/foo.git",
                "IdentityFile": "/dev/null",
            }]))


class TestGitEntryCrossValidation(unittest.TestCase):
    def test_duplicate_name_dies(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"git": {"remotes": {
                    "a.example": {
                        "Name": "foo",
                        "Upstream": "ssh://git@a.example/x.git",
                        "IdentityFile": "/dev/null",
                    },
                    "b.example": {
                        "Name": "foo",
                        "Upstream": "ssh://git@b.example/y.git",
                        "IdentityFile": "/dev/null",
                    },
                }}}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_remote_key_must_match_upstream_host(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"git": {"remotes": {
                    "wrong.example": {
                        "Name": "foo",
                        "Upstream": "ssh://git@github.com/foo.git",
                        "IdentityFile": "/dev/null",
                    },
                }}}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_remote_key_can_name_logical_host_for_ip_upstream(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {"git": {"remotes": {
                "gitea.dideric.is": {
                    "Name": "bot-bottle",
                    "Upstream": "ssh://git@192.0.2.10:30009/didericis/bot-bottle.git",
                    "IdentityFile": "/dev/null",
                },
            }}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        e = m.bottles["dev"].git[0]
        self.assertEqual("gitea.dideric.is", e.RemoteKey)
        self.assertEqual("192.0.2.10", e.UpstreamHost)
        self.assertEqual("30009", e.UpstreamPort)

    def test_legacy_ssh_field_dies_with_hint(self):
        # PRD 0009: bottle.ssh is removed; manifests carrying it must
        # fail loudly with a hint pointing at bottle.git.
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


class TestEmptyGitField(unittest.TestCase):
    def test_no_git_field_yields_empty_tuple(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)

    def test_git_object_type_required(self):
        with self.assertRaises(ManifestError):
            Manifest.from_json_obj({
                "bottles": {"dev": {"git": "not-a-list"}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_empty_remotes_yields_empty_tuple(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {"git": {"remotes": {}}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)


if __name__ == "__main__":
    unittest.main()
