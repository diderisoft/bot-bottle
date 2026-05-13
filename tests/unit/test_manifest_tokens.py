"""Unit: Bottle.tokens manifest parsing + validation (PRD 0010)."""

import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Manifest


def _manifest(tokens, git=None):
    bottle: dict[str, object] = {"tokens": tokens}
    if git is not None:
        bottle["git"] = git
    return {
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestTokenEntryParsing(unittest.TestCase):
    def test_parses_anthropic_entry(self):
        m = Manifest.from_json_obj(_manifest([
            {"Kind": "anthropic", "TokenRef": "CLAUDE_BOTTLE_OAUTH_TOKEN"},
        ]))
        entries = m.bottles["dev"].tokens
        self.assertEqual(1, len(entries))
        e = entries[0]
        self.assertEqual("anthropic", e.Kind)
        self.assertEqual("CLAUDE_BOTTLE_OAUTH_TOKEN", e.TokenRef)
        self.assertEqual("", e.Url)
        self.assertEqual("api.anthropic.com", e.UpstreamHost)

    def test_parses_github_entry(self):
        m = Manifest.from_json_obj(_manifest([
            {"Kind": "github", "TokenRef": "GITHUB_TOKEN"},
        ]))
        e = m.bottles["dev"].tokens[0]
        self.assertEqual("github", e.Kind)
        self.assertEqual("github.com", e.UpstreamHost)

    def test_parses_npm_entry(self):
        m = Manifest.from_json_obj(_manifest([
            {"Kind": "npm", "TokenRef": "NPM_TOKEN"},
        ]))
        e = m.bottles["dev"].tokens[0]
        self.assertEqual("registry.npmjs.org", e.UpstreamHost)

    def test_parses_gitea_entry_with_url(self):
        m = Manifest.from_json_obj(_manifest([
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is"},
        ]))
        e = m.bottles["dev"].tokens[0]
        self.assertEqual("gitea", e.Kind)
        self.assertEqual("https://gitea.dideric.is", e.Url)
        self.assertEqual("gitea.dideric.is", e.UpstreamHost)

    def test_gitea_url_with_port_strips_port_from_host(self):
        m = Manifest.from_json_obj(_manifest([
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is:30009"},
        ]))
        self.assertEqual("gitea.dideric.is", m.bottles["dev"].tokens[0].UpstreamHost)


class TestTokenEntryValidation(unittest.TestCase):
    def test_unknown_kind_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "aws", "TokenRef": "AWS_TOKEN"},
            ]))

    def test_missing_kind_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"TokenRef": "GITHUB_TOKEN"},
            ]))

    def test_missing_token_ref_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "github"},
            ]))

    def test_gitea_without_url_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "gitea", "TokenRef": "GITEA_TOKEN"},
            ]))

    def test_gitea_with_non_https_url_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
                 "Url": "http://gitea.dideric.is"},
            ]))

    def test_non_gitea_kind_with_url_dies(self):
        # Url is fixed for anthropic / github / npm — passing one is a
        # configuration smell, not an override knob.
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "github", "TokenRef": "GITHUB_TOKEN",
                 "Url": "https://api.example.com"},
            ]))

    def test_duplicate_non_gitea_kind_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "github", "TokenRef": "A"},
                {"Kind": "github", "TokenRef": "B"},
            ]))

    def test_two_gitea_with_distinct_urls_ok(self):
        m = Manifest.from_json_obj(_manifest([
            {"Kind": "gitea", "TokenRef": "T1",
             "Url": "https://gitea.dideric.is"},
            {"Kind": "gitea", "TokenRef": "T2",
             "Url": "https://gitea.example.com"},
        ]))
        self.assertEqual(2, len(m.bottles["dev"].tokens))

    def test_two_gitea_with_same_url_dies(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest([
                {"Kind": "gitea", "TokenRef": "T1",
                 "Url": "https://gitea.dideric.is"},
                {"Kind": "gitea", "TokenRef": "T2",
                 "Url": "https://gitea.dideric.is"},
            ]))


class TestTokenGitCoexistence(unittest.TestCase):
    """git-gate brokers SSH push/fetch via an IdentityFile; cred-proxy
    brokers HTTPS REST API calls via a PAT. Declaring both on the same
    host is the common dev setup (SSH key for git ops, PAT for `tea` /
    `gh` API calls), not a configuration error."""

    def test_github_token_and_github_git_entry_coexist(self):
        m = Manifest.from_json_obj(_manifest(
            tokens=[{"Kind": "github", "TokenRef": "GITHUB_TOKEN"}],
            git=[{
                "Name": "myrepo",
                "Upstream": "ssh://git@github.com/me/myrepo.git",
                "IdentityFile": "/dev/null",
            }],
        ))
        self.assertEqual(1, len(m.bottles["dev"].tokens))
        self.assertEqual(1, len(m.bottles["dev"].git))

    def test_gitea_token_and_same_host_git_entry_coexist(self):
        m = Manifest.from_json_obj(_manifest(
            tokens=[{
                "Kind": "gitea", "TokenRef": "GITEA_TOKEN",
                "Url": "https://gitea.dideric.is",
            }],
            git=[{
                "Name": "myrepo",
                "Upstream": "ssh://git@gitea.dideric.is:30009/me/myrepo.git",
                "IdentityFile": "/dev/null",
            }],
        ))
        self.assertEqual("gitea.dideric.is", m.bottles["dev"].tokens[0].UpstreamHost)
        self.assertEqual("gitea.dideric.is", m.bottles["dev"].git[0].UpstreamHost)

    def test_anthropic_token_and_git_unrelated(self):
        # api.anthropic.com isn't a git host; coexistence is trivial.
        m = Manifest.from_json_obj(_manifest(
            tokens=[{"Kind": "anthropic", "TokenRef": "CLAUDE_BOTTLE_OAUTH_TOKEN"}],
            git=[{
                "Name": "myrepo",
                "Upstream": "ssh://git@gitea.dideric.is:30009/me/myrepo.git",
                "IdentityFile": "/dev/null",
            }],
        ))
        self.assertEqual(1, len(m.bottles["dev"].tokens))


class TestEmptyTokensField(unittest.TestCase):
    def test_no_tokens_field_yields_empty_tuple(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertEqual((), m.bottles["dev"].tokens)

    def test_tokens_array_type_required(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj({
                "bottles": {"dev": {"tokens": "not-a-list"}},
                "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
            })


if __name__ == "__main__":
    unittest.main()
