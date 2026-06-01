"""Unit: pipelock_effective_allowlist — pipelock's allowlist
mirrors manifest-declared egress routes. Git upstreams declared in
`bottle.git` don't contribute; they flow through the per-agent
git-gate (PRD 0008)."""

import unittest

from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import (
    pipelock_effective_allowlist,
    pipelock_effective_ssrf_ip_allowlist,
    pipelock_effective_tls_passthrough,
)


def _bottle(spec):
    return Manifest.from_json_obj({
        "bottles": {"dev": spec},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _routes(routes):
    return {"egress": {"routes": routes}}


class TestEffectiveAllowlist(unittest.TestCase):
    def test_empty_without_any_manifest_routes(self):
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertEqual([], eff)

    def test_sorted_and_deduped(self):
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "api.anthropic.com",
             "auth": {"scheme": "Bearer", "token_ref": "T"}},
        ])))
        self.assertEqual(len(eff), len(set(eff)))
        self.assertEqual(eff, sorted(eff))


class TestAllowlistWithRoutes(unittest.TestCase):
    def test_manifest_route_hosts_present(self):
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "registry.npmjs.org",
             "auth": {"scheme": "Bearer", "token_ref": "N"}},
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "G"}},
        ])))
        self.assertIn("registry.npmjs.org", eff)
        self.assertIn("api.github.com", eff)

    def test_no_baked_defaults_alongside_manifest_routes(self):
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "x.example",
             "auth": {"scheme": "Bearer", "token_ref": "T"}},
        ])))
        self.assertEqual(["x.example"], eff)

    def test_egress_hostname_NOT_in_pipelock_allowlist(self):
        # The agent never dials egress via the proxy mechanism
        # — it IS the proxy. Pipelock receives upstream hostnames
        # from egress's CONNECT requests, not the
        # `egress` hostname itself.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "x.example",
             "auth": {"scheme": "Bearer", "token_ref": "T"}},
        ])))
        self.assertNotIn("egress", eff)

    def test_supervise_hostname_auto_added_when_supervise_enabled(self):
        eff = pipelock_effective_allowlist(_bottle({"supervise": True}))
        self.assertIn("supervise", eff)

    def test_supervise_hostname_NOT_added_when_disabled(self):
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertNotIn("supervise", eff)
        eff_explicit = pipelock_effective_allowlist(_bottle({"supervise": False}))
        self.assertNotIn("supervise", eff_explicit)

    def test_path_allowlist_does_not_affect_pipelock_allowlist(self):
        # path_allowlist is enforced by egress, not pipelock.
        # Pipelock only sees the upstream hostname; the path filter
        # has already passed (or 403'd) at egress.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "github.com", "path_allowlist": ["/x/", "/y/"]},
        ])))
        self.assertIn("github.com", eff)
        for entry in eff:
            self.assertFalse(entry.startswith("/"))


class TestTlsPassthrough(unittest.TestCase):
    def test_default_empty(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle({}))
        self.assertEqual([], passthrough)

    def test_route_hosts_not_added_to_passthrough_by_default(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle(_routes([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "G"}},
            {"host": "registry.npmjs.org",
             "auth": {"scheme": "Bearer", "token_ref": "N"}},
        ])))
        self.assertEqual([], passthrough)

    def test_route_policy_adds_tls_passthrough(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle(_routes([
            {"host": "api.openai.com",
             "auth": {"scheme": "Bearer", "token_ref": "O"},
             "pipelock": {"tls_passthrough": True}},
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "G"}},
        ])))
        self.assertEqual(["api.openai.com"], passthrough)

    def test_forward_host_credentials_passes_through_codex_hosts(self):
        # Egress injects the host bearer on the Codex API hosts; pipelock
        # must pass them through or its header DLP blocks the injected JWT
        # ("request header contains secret"). These routes are auto-added
        # (not in bottle.egress.routes), so passthrough is host-derived.
        passthrough = pipelock_effective_tls_passthrough(_bottle({
            "agent_provider": {
                "template": "codex",
                "forward_host_credentials": True,
            },
        }))
        self.assertEqual(["api.openai.com", "chatgpt.com"], passthrough)

    def test_no_codex_passthrough_without_forward_host_credentials(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle({
            "agent_provider": {"template": "codex"},
        }))
        self.assertEqual([], passthrough)


class TestSsrfIpAllowlist(unittest.TestCase):
    def test_default_empty(self):
        allowlist = pipelock_effective_ssrf_ip_allowlist(_bottle({}))
        self.assertEqual([], allowlist)

    def test_route_policy_adds_ssrf_ip_allowlist(self):
        allowlist = pipelock_effective_ssrf_ip_allowlist(_bottle(_routes([
            {"host": "gitea.dideric.is",
             "auth": {"scheme": "token", "token_ref": "G"},
             "pipelock": {"ssrf_ip_allowlist": ["192.0.2.10/32"]}},
        ])))
        self.assertEqual(["192.0.2.10/32"], allowlist)

    def test_route_policy_merges_with_extra(self):
        allowlist = pipelock_effective_ssrf_ip_allowlist(
            _bottle(_routes([
                {"host": "gitea.dideric.is",
                 "pipelock": {"ssrf_ip_allowlist": ["192.0.2.10/32"]}},
            ])),
            ("172.20.0.0/16",),
        )
        self.assertEqual(["192.0.2.10/32", "172.20.0.0/16"], allowlist)


if __name__ == "__main__":
    unittest.main()
