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


def _bottle(routes):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"cred_proxy": {"routes": routes}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _upstreams(routes):
    return cred_proxy_upstreams_for_bottle(_bottle(routes))


class TestRenderNpmrc(unittest.TestCase):
    def test_empty_when_no_role(self):
        self.assertEqual("", render_npmrc(_upstreams([])))
        self.assertEqual("", render_npmrc(_upstreams([
            {"path": "/x/", "upstream": "https://x.example",
             "auth_scheme": "Bearer", "token_ref": "T"},
        ])))

    def test_writes_registry_line_for_npm_registry_role(self):
        out = render_npmrc(_upstreams([
            {"path": "/npm/", "upstream": "https://registry.npmjs.org",
             "auth_scheme": "Bearer", "token_ref": "NPM_TOKEN",
             "role": "npm-registry"},
        ]))
        self.assertEqual("registry=http://cred-proxy:9099/npm/\n", out)

    def test_omits_authtoken(self):
        # The proxy injects Authorization at request time.
        out = render_npmrc(_upstreams([
            {"path": "/npm/", "upstream": "https://registry.npmjs.org",
             "auth_scheme": "Bearer", "token_ref": "NPM_TOKEN",
             "role": "npm-registry"},
        ]))
        self.assertNotIn("_authToken", out)
        self.assertNotIn("NPM_TOKEN", out)


class TestRenderGitconfig(unittest.TestCase):
    def test_empty_when_no_role(self):
        self.assertEqual("", render_cred_proxy_gitconfig(_upstreams([
            {"path": "/anthropic/", "upstream": "https://api.anthropic.com",
             "auth_scheme": "Bearer", "token_ref": "A"},
        ])))

    def test_writes_insteadof_for_git_insteadof_role(self):
        out = render_cred_proxy_gitconfig(_upstreams([
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GH",
             "role": "git-insteadof"},
        ]))
        self.assertIn('[url "http://cred-proxy:9099/gh-git/"]', out)
        self.assertIn("insteadOf = https://github.com/", out)

    def test_gitea_writes_per_host_insteadof(self):
        out = render_cred_proxy_gitconfig(_upstreams([
            {"path": "/gitea/dideric/", "upstream": "https://gitea.dideric.is",
             "auth_scheme": "token", "token_ref": "GITEA",
             "role": "git-insteadof"},
        ]))
        self.assertIn('[url "http://cred-proxy:9099/gitea/dideric/"]', out)
        self.assertIn("insteadOf = https://gitea.dideric.is/", out)

    def test_two_routes_yield_two_rules(self):
        out = render_cred_proxy_gitconfig(_upstreams([
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GH",
             "role": "git-insteadof"},
            {"path": "/gitea/x/", "upstream": "https://gitea.example.com",
             "auth_scheme": "token", "token_ref": "GT",
             "role": "git-insteadof"},
        ]))
        self.assertEqual(2, out.count("insteadOf"))
        self.assertIn("github.com", out)
        self.assertIn("gitea.example.com", out)

    def test_suppressed_when_git_gate_covers_host(self):
        # When bottle.git brokers github.com over SSH, git-gate is the
        # canonical git path. The cred-proxy https://github.com/
        # rewrite would let the agent push over HTTPS — bypassing
        # gitleaks. Suppress it.
        out = render_cred_proxy_gitconfig(
            _upstreams([
                {"path": "/gh-git/", "upstream": "https://github.com",
                 "auth_scheme": "Bearer", "token_ref": "GH",
                 "role": "git-insteadof"},
            ]),
            {"github.com"},
        )
        self.assertEqual("", out)

    def test_partial_suppression_keeps_other_hosts(self):
        out = render_cred_proxy_gitconfig(
            _upstreams([
                {"path": "/gitea/a/", "upstream": "https://gitea.dideric.is",
                 "auth_scheme": "token", "token_ref": "T1",
                 "role": "git-insteadof"},
                {"path": "/gitea/b/", "upstream": "https://gitea.example.com",
                 "auth_scheme": "token", "token_ref": "T2",
                 "role": "git-insteadof"},
            ]),
            {"gitea.dideric.is"},
        )
        self.assertNotIn("gitea.dideric.is/", out)
        self.assertIn("gitea.example.com/", out)


class TestRenderTeaConfig(unittest.TestCase):
    def test_empty_when_no_role(self):
        self.assertEqual("", render_tea_config(_upstreams([
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "G"},
        ])))

    def test_single_login_block(self):
        out = render_tea_config(_upstreams([
            {"path": "/gitea/dideric/", "upstream": "https://gitea.dideric.is",
             "auth_scheme": "token", "token_ref": "GITEA",
             "role": "tea-login"},
        ]))
        self.assertIn("logins:", out)
        # Login name comes from the upstream host, not the path —
        # the path may not encode the host.
        self.assertIn("- name: gitea.dideric.is", out)
        self.assertIn("url: http://cred-proxy:9099/gitea/dideric/", out)
        self.assertIn("token: cred-proxy-placeholder", out)
        self.assertNotIn("GITEA", out)


class TestCombinedRoles(unittest.TestCase):
    """A single gitea route typically carries both `git-insteadof`
    and `tea-login` — the renderers should each fire independently."""

    def test_gitea_route_fires_both_renderers(self):
        routes = _upstreams([
            {"path": "/gitea/x/", "upstream": "https://gitea.example.com",
             "auth_scheme": "token", "token_ref": "T",
             "role": ["git-insteadof", "tea-login"]},
        ])
        self.assertIn("insteadOf", render_cred_proxy_gitconfig(routes))
        self.assertIn("logins:", render_tea_config(routes))


if __name__ == "__main__":
    unittest.main()
