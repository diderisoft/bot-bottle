"""Unit: git-gate.repos manifest parsing + validation (PRD 0047)."""

import unittest

from bot_bottle.manifest import ManifestError, ManifestIndex


def _manifest(repos: dict) -> dict:  # type: ignore
    return {
        "bottles": {"dev": {"git-gate": {"repos": repos}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestGitEntryParsing(unittest.TestCase):
    def test_parses_minimal_entry(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "bot-bottle": {
                "url": "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
                "key": {"provider": "static", "path": "/dev/null"},
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
        m = ManifestIndex.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/didericis/foo.git",
                "key": {"provider": "static", "path": "/dev/null"},
            },
        }))
        e = m.bottles["dev"].git[0]
        self.assertEqual("22", e.UpstreamPort)
        self.assertEqual("github.com", e.UpstreamHost)

    def test_host_key_optional(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/foo.git",
                "key": {"provider": "static", "path": "/dev/null"},
            },
        }))
        self.assertEqual("", m.bottles["dev"].git[0].KnownHostKey)

    def test_host_key_stored(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/foo.git",
                "key": {"provider": "static", "path": "/dev/null"},
                "host_key": "ssh-ed25519 AAAA",
            },
        }))
        self.assertEqual("ssh-ed25519 AAAA", m.bottles["dev"].git[0].KnownHostKey)

    def test_repo_name_becomes_Name(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "my-repo": {
                "url": "ssh://git@github.com/foo.git",
                "key": {"provider": "static", "path": "/dev/null"},
            },
        }))
        self.assertEqual("my-repo", m.bottles["dev"].git[0].Name)

    def test_missing_url_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {"key": {"provider": "static", "path": "/dev/null"}},
            }))

    def test_missing_key_block_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {"url": "ssh://git@github.com/foo.git"},
            }))

    def test_unknown_key_in_entry_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                    "IdentityFile": "/dev/null",  # old PascalCase key
                },
            }))

    def test_non_ssh_url_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "https://github.com/didericis/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_scp_style_url_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "git@github.com:didericis/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_url_without_user_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_url_without_path_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_non_numeric_port_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com:notaport/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_ip_literal_upstream(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "bot-bottle": {
                "url": "ssh://git@192.0.2.10:30009/didericis/bot-bottle.git",
                "key": {"provider": "static", "path": "/dev/null"},
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
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"git-gate": {"repos": {
                "foo": {
                    "url": "ssh://git@a.example/x.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
                "bar": {
                    "url": "ssh://git@b.example/y.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        names = {e.Name for e in m.bottles["dev"].git}
        self.assertEqual({"foo", "bar"}, names)

    def test_legacy_ssh_field_dies_with_hint(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
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

    def test_name_with_single_quote_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "o'reilly": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_name_with_space_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "my repo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_name_with_semicolon_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo;bar": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_name_with_dollar_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo$bar": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }))

    def test_valid_name_with_dots_and_hyphens_accepted(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "my.repo-name_1": {
                "url": "ssh://git@github.com/foo.git",
                "key": {"provider": "static", "path": "/dev/null"},
            },
        }))
        self.assertEqual("my.repo-name_1", m.bottles["dev"].git[0].Name)

    def test_legacy_git_key_dies_with_hint(self):
        msg = ""
        try:
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"git": {"remotes": {}}}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })
        except ManifestError as e:
            msg = str(e)
        self.assertIn("git-gate", msg)
        self.assertIn("PRD 0047", msg)


class TestStaticKey(unittest.TestCase):
    """git-gate.repos entries with key.provider = "static"."""

    def test_static_key_minimal(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "bot-bottle": {
                "url": "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
                "key": {"provider": "static", "path": "/home/user/.ssh/id_ed25519"},
            },
        }))
        e = m.bottles["dev"].git[0]
        self.assertEqual("bot-bottle", e.Name)
        self.assertEqual("static", e.Key.provider)
        self.assertEqual("/home/user/.ssh/id_ed25519", e.Key.path)
        self.assertEqual("/home/user/.ssh/id_ed25519", e.IdentityFile)

    def test_static_key_sets_identity_file_at_parse_time(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/foo.git",
                "key": {"provider": "static", "path": "/dev/null"},
            },
        }))
        self.assertEqual("/dev/null", m.bottles["dev"].git[0].IdentityFile)

    def test_static_key_missing_path_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static"},
                },
            }))

    def test_static_key_unknown_field_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "static", "path": "/dev/null", "api_url": "x"},
                },
            }))


class TestGiteaKey(unittest.TestCase):
    """git-gate.repos entries with key.provider = "gitea"."""

    def test_gitea_key_minimal(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "bot-bottle": {
                "url": "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
                "key": {
                    "provider": "gitea",
                    "forge_token_env": "GITEA_TOKEN",
                },
            },
        }))
        e = m.bottles["dev"].git[0]
        self.assertEqual("bot-bottle", e.Name)
        self.assertEqual("gitea", e.Key.provider)
        self.assertEqual("GITEA_TOKEN", e.Key.forge_token_env)
        self.assertEqual("", e.Key.api_url)
        self.assertEqual("", e.IdentityFile)

    def test_gitea_key_with_api_url(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "repo": {
                "url": "ssh://git@gitea.example.com/org/repo.git",
                "key": {
                    "provider": "gitea",
                    "forge_token_env": "MY_TOKEN",
                    "api_url": "https://gitea.example.com",
                },
            },
        }))
        self.assertEqual("https://gitea.example.com", m.bottles["dev"].git[0].Key.api_url)

    def test_gitea_key_has_no_identity_file_at_parse_time(self):
        m = ManifestIndex.from_json_obj(_manifest({
            "foo": {
                "url": "ssh://git@github.com/didericis/foo.git",
                "key": {"provider": "gitea", "forge_token_env": "T"},
            },
        }))
        self.assertEqual("", m.bottles["dev"].git[0].IdentityFile)

    def test_gitea_key_missing_forge_token_env_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "gitea"},
                },
            }))

    def test_gitea_key_unknown_field_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {
                        "provider": "gitea",
                        "forge_token_env": "T",
                        "key_type": "rsa",  # not allowed
                    },
                },
            }))


class TestKeyBlockValidation(unittest.TestCase):
    """Validation rules on the key block shared across providers."""

    def test_missing_provider_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"path": "/dev/null"},
                },
            }))

    def test_unknown_provider_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {
                    "url": "ssh://git@github.com/foo.git",
                    "key": {"provider": "github"},
                },
            }))

    def test_missing_key_block_dies(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj(_manifest({
                "foo": {"url": "ssh://git@github.com/foo.git"},
            }))


class TestEmptyGitGateField(unittest.TestCase):
    def test_no_git_gate_field_yields_empty_tuple(self):
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)

    def test_git_gate_object_type_required(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"git-gate": "not-a-dict"}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })

    def test_empty_repos_yields_empty_tuple(self):
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"git-gate": {"repos": {}}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].git)


if __name__ == "__main__":
    unittest.main()
