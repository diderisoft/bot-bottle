"""Unit: pipelock_effective_allowlist — pipelock's allowlist
mirrors `egress_proxy_routes_for_bottle` (which folds in
DEFAULT_ALLOWLIST). Git upstreams declared in `bottle.git` don't
contribute; they flow through the per-agent git-gate (PRD 0008)."""

import unittest

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import (
    pipelock_effective_allowlist,
    pipelock_effective_tls_passthrough,
)


def _bottle(spec):
    return Manifest.from_json_obj({
        "bottles": {"dev": spec},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _routes(routes):
    return {"egress_proxy": {"routes": routes}}


class TestEffectiveAllowlist(unittest.TestCase):
    def test_default_allowlist_present_without_any_manifest_routes(self):
        # No egress_proxy routes declared → pipelock allowlist is
        # just the baked DEFAULT_ALLOWLIST (folded in by
        # egress_proxy_routes_for_bottle).
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertIn("api.anthropic.com", eff)
        self.assertIn("sentry.io", eff)

    def test_sorted_and_deduped(self):
        # Manifest route for a default host collapses to one entry.
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

    def test_baked_defaults_still_present_alongside_manifest_routes(self):
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "x.example",
             "auth": {"scheme": "Bearer", "token_ref": "T"}},
        ])))
        for default in ("api.anthropic.com", "sentry.io"):
            self.assertIn(default, eff)
        self.assertIn("x.example", eff)

    def test_egress_proxy_hostname_NOT_in_pipelock_allowlist(self):
        # The agent never dials egress-proxy via the proxy mechanism
        # — it IS the proxy. Pipelock receives upstream hostnames
        # from egress-proxy's CONNECT requests, not the
        # `egress-proxy` hostname itself.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "x.example",
             "auth": {"scheme": "Bearer", "token_ref": "T"}},
        ])))
        self.assertNotIn("egress-proxy", eff)

    def test_supervise_hostname_auto_added_when_supervise_enabled(self):
        eff = pipelock_effective_allowlist(_bottle({"supervise": True}))
        self.assertIn("supervise", eff)

    def test_supervise_hostname_NOT_added_when_disabled(self):
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertNotIn("supervise", eff)
        eff_explicit = pipelock_effective_allowlist(_bottle({"supervise": False}))
        self.assertNotIn("supervise", eff_explicit)

    def test_path_allowlist_does_not_affect_pipelock_allowlist(self):
        # path_allowlist is enforced by egress-proxy, not pipelock.
        # Pipelock only sees the upstream hostname; the path filter
        # has already passed (or 403'd) at egress-proxy.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "github.com", "path_allowlist": ["/x/", "/y/"]},
        ])))
        self.assertIn("github.com", eff)
        for entry in eff:
            self.assertFalse(entry.startswith("/"))


class TestTlsPassthrough(unittest.TestCase):
    def test_default_includes_api_anthropic(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle({}))
        self.assertEqual(["api.anthropic.com"], passthrough)

    def test_route_hosts_NOT_added_to_passthrough(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle(_routes([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "G"}},
            {"host": "registry.npmjs.org",
             "auth": {"scheme": "Bearer", "token_ref": "N"}},
        ])))
        self.assertEqual(["api.anthropic.com"], passthrough)


if __name__ == "__main__":
    unittest.main()
