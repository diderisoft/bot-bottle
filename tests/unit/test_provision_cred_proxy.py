"""Unit: cred-proxy agent-side provisioner renderers (PRD 0010).

The docker cp / docker exec side effects are exercised by integration
tests; these unit tests cover the pure render functions."""

import unittest

from claude_bottle.backend.docker.provision.cred_proxy import (
    render_cred_proxy_gitconfig,
    render_npmrc,
    render_tea_config,
)
from claude_bottle.cred_proxy import cred_proxy_upstreams_for_bottle
from claude_bottle.manifest import Manifest


def _bottle(tokens):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"tokens": tokens}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _upstreams(tokens):
    return cred_proxy_upstreams_for_bottle(_bottle(tokens))


class TestRenderNpmrc(unittest.TestCase):
    def test_empty_when_no_npm_route(self):
        self.assertEqual("", render_npmrc(_upstreams([])))
        self.assertEqual("", render_npmrc(_upstreams([
            {"Kind": "anthropic", "TokenRef": "A"},
        ])))

    def test_writes_registry_line(self):
        out = render_npmrc(_upstreams([
            {"Kind": "npm", "TokenRef": "NPM_TOKEN"},
        ]))
        self.assertEqual("registry=http://cred-proxy:9099/npm/\n", out)

    def test_omits_authtoken(self):
        # The proxy injects Authorization at request time. The npmrc
        # deliberately carries no _authToken — a stale token there
        # would just get stripped, but it also creates the false
        # impression that the agent holds a credential.
        out = render_npmrc(_upstreams([
            {"Kind": "npm", "TokenRef": "NPM_TOKEN"},
        ]))
        self.assertNotIn("_authToken", out)
        self.assertNotIn("NPM_TOKEN", out)


class TestRenderGitconfig(unittest.TestCase):
    def test_empty_when_no_github_or_gitea(self):
        self.assertEqual("", render_cred_proxy_gitconfig(_upstreams([
            {"Kind": "anthropic", "TokenRef": "A"},
            {"Kind": "npm", "TokenRef": "N"},
        ])))

    def test_github_writes_https_insteadof(self):
        out = render_cred_proxy_gitconfig(_upstreams([
            {"Kind": "github", "TokenRef": "GITHUB_TOKEN"},
        ]))
        self.assertIn('[url "http://cred-proxy:9099/gh-git/"]', out)
        self.assertIn("insteadOf = https://github.com/", out)

    def test_gitea_writes_per_host_insteadof(self):
        out = render_cred_proxy_gitconfig(_upstreams([
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is"},
        ]))
        self.assertIn('[url "http://cred-proxy:9099/gitea/gitea.dideric.is/"]', out)
        self.assertIn("insteadOf = https://gitea.dideric.is/", out)

    def test_two_giteas_yield_two_rules(self):
        out = render_cred_proxy_gitconfig(_upstreams([
            {"Kind": "gitea", "TokenRef": "G1",
             "Url": "https://gitea.dideric.is"},
            {"Kind": "gitea", "TokenRef": "G2",
             "Url": "https://gitea.example.com"},
        ]))
        self.assertEqual(2, out.count("insteadOf"))
        self.assertIn("gitea.dideric.is/", out)
        self.assertIn("gitea.example.com/", out)


class TestRenderTeaConfig(unittest.TestCase):
    def test_empty_when_no_gitea(self):
        self.assertEqual("", render_tea_config(_upstreams([
            {"Kind": "github", "TokenRef": "G"},
        ])))

    def test_single_gitea_login_block(self):
        out = render_tea_config(_upstreams([
            {"Kind": "gitea", "TokenRef": "GITEA_TOKEN",
             "Url": "https://gitea.dideric.is"},
        ]))
        self.assertIn("logins:", out)
        self.assertIn("- name: gitea.dideric.is", out)
        self.assertIn("url: http://cred-proxy:9099/gitea/gitea.dideric.is/", out)
        # Placeholder token, not the host env var name (which is not a
        # secret but also not useful) or the real value (which the
        # provisioner does not have).
        self.assertIn("token: cred-proxy-placeholder", out)
        self.assertNotIn("GITEA_TOKEN", out)


if __name__ == "__main__":
    unittest.main()
