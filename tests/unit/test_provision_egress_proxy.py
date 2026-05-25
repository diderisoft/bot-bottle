"""Unit: agent-side provisioning for egress-proxy roles (PRD 0017).

Each role drives one dotfile / env-var rewrite at bottle bring-up.
HTTPS_PROXY routes the canonical URL through egress-proxy, which
injects auth and DLP-scans on the upstream leg."""

import unittest

from claude_bottle.backend.docker.provision.egress_proxy import (
    render_npmrc,
    render_tea_config,
)
from claude_bottle.egress_proxy import egress_proxy_routes_for_bottle
from claude_bottle.manifest import Manifest


def _routes(manifest_routes):
    m = Manifest.from_json_obj({
        "bottles": {"dev": {"egress_proxy": {"routes": manifest_routes}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })
    return egress_proxy_routes_for_bottle(m.bottles["dev"])


# --- npmrc -----------------------------------------------------------


class TestRenderNpmrc(unittest.TestCase):
    def test_canonical_upstream_url(self):
        routes = _routes([
            {"host": "registry.npmjs.org", "role": "npm-registry",
             "auth": {"scheme": "Bearer", "token_ref": "NPM_TOKEN"}},
        ])
        self.assertEqual(
            "registry=https://registry.npmjs.org/\n",
            render_npmrc(routes),
        )

    def test_empty_when_no_npm_role(self):
        routes = _routes([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH"}},
        ])
        self.assertEqual("", render_npmrc(routes))

    def test_no_routes_empty(self):
        self.assertEqual("", render_npmrc(()))

    def test_no_auth_token_in_npmrc(self):
        # The proxy injects auth; the npmrc must carry no secret —
        # not even `:always-auth=true` lines that would prompt npm
        # to wait for credentials. Just the registry URL.
        routes = _routes([
            {"host": "registry.npmjs.org", "role": "npm-registry",
             "auth": {"scheme": "Bearer", "token_ref": "NPM_TOKEN"}},
        ])
        out = render_npmrc(routes)
        self.assertNotIn("_authToken", out)
        self.assertNotIn("NPM_TOKEN", out)


# --- tea config ------------------------------------------------------


class TestRenderTeaConfig(unittest.TestCase):
    def test_single_login(self):
        routes = _routes([
            {"host": "gitea.dideric.is", "role": "tea-login",
             "auth": {"scheme": "token", "token_ref": "GITEA_TOKEN"}},
        ])
        out = render_tea_config(routes)
        self.assertIn("- name: gitea.dideric.is", out)
        self.assertIn("url: https://gitea.dideric.is", out)
        self.assertIn("token: egress-proxy-placeholder", out)

    def test_multiple_logins_each_get_own_entry(self):
        routes = _routes([
            {"host": "gitea.a.example", "role": "tea-login",
             "auth": {"scheme": "token", "token_ref": "T_A"}},
            {"host": "gitea.b.example", "role": "tea-login",
             "auth": {"scheme": "token", "token_ref": "T_B"}},
        ])
        out = render_tea_config(routes)
        self.assertIn("- name: gitea.a.example", out)
        self.assertIn("- name: gitea.b.example", out)

    def test_empty_when_no_tea_role(self):
        routes = _routes([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH"}},
        ])
        self.assertEqual("", render_tea_config(routes))

    def test_no_routes_empty(self):
        self.assertEqual("", render_tea_config(()))

    def test_no_real_token_in_config(self):
        routes = _routes([
            {"host": "gitea.dideric.is", "role": "tea-login",
             "auth": {"scheme": "token", "token_ref": "GITEA_TOKEN"}},
        ])
        out = render_tea_config(routes)
        # GITEA_TOKEN is just the env var name, not the value —
        # placeholder-only is the SC.
        self.assertIn("egress-proxy-placeholder", out)


if __name__ == "__main__":
    unittest.main()
