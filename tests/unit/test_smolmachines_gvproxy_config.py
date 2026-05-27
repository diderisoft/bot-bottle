"""Unit: gvproxy YAML renderer for the smolmachines backend
(PRD 0023). The config shape comes from the recipe in
`docs/research/agent-vm-isolation.md` § "Full Setup". Tests pin
the load-bearing rules: only `proxy.internal` resolves; only
explicit port forwards are reachable."""

from __future__ import annotations

import unittest

from claude_bottle.backend.smolmachines.gvproxy_config import (
    PortForward,
    gvproxy_config_build,
    gvproxy_config_render,
)


class TestGvproxyConfigBuild(unittest.TestCase):
    def test_subnet_and_gateway_pass_through(self):
        cfg = gvproxy_config_build(
            subnet="192.168.50.0/24",
            gateway="192.168.50.1",
            port_forwards=(),
        )
        self.assertEqual("192.168.50.0/24", cfg["subnet"])
        self.assertEqual("192.168.50.1", cfg["gateway"])

    def test_dns_resolves_only_proxy_internal(self):
        # Load-bearing for PRD 0022's DNS-exfil attack: anything
        # other than `proxy.internal` MUST return NXDOMAIN.
        cfg = gvproxy_config_build(
            subnet="192.168.50.0/24",
            gateway="192.168.50.1",
            port_forwards=(),
        )
        self.assertEqual(1, len(cfg["dns"]))
        zone = cfg["dns"][0]
        self.assertEqual(".", zone["zone"])
        self.assertEqual(
            [{"name": "proxy.internal", "ip": "192.168.50.1"}],
            zone["records"],
        )

    def test_port_forwards_render_one_per_entry(self):
        cfg = gvproxy_config_build(
            subnet="192.168.50.0/24",
            gateway="192.168.50.1",
            port_forwards=(
                PortForward(gateway_port=8888, host_port=51001),
                PortForward(gateway_port=8889, host_port=51002),
                PortForward(gateway_port=8890, host_port=51003),
            ),
        )
        self.assertEqual(3, len(cfg["port_forwards"]))
        # All forwards land on host loopback.
        for pf in cfg["port_forwards"]:
            self.assertEqual("127.0.0.1", pf["host"])

    def test_no_port_forwards_renders_empty_list(self):
        # A bottle that somehow had no forwards (none in practice
        # since pipelock is always allocated) must not silently
        # default to permissive — explicit empty list keeps the
        # guest with literally no outbound destinations.
        cfg = gvproxy_config_build(
            subnet="192.168.50.0/24",
            gateway="192.168.50.1",
            port_forwards=(),
        )
        self.assertEqual([], cfg["port_forwards"])


class TestGvproxyConfigRender(unittest.TestCase):
    def _render(self, **kwargs):
        defaults = dict(
            subnet="192.168.50.0/24",
            gateway="192.168.50.1",
            port_forwards=(PortForward(gateway_port=8888, host_port=51001),),
        )
        defaults.update(kwargs)
        return gvproxy_config_render(gvproxy_config_build(**defaults))

    def test_subnet_and_gateway_quoted_strings(self):
        text = self._render()
        self.assertIn('subnet: "192.168.50.0/24"', text)
        self.assertIn('gateway: "192.168.50.1"', text)

    def test_dns_records_emit_in_yaml_list_form(self):
        text = self._render()
        self.assertIn('dns:', text)
        self.assertIn('- zone: "."', text)
        self.assertIn('- name: "proxy.internal"', text)
        self.assertIn('ip: "192.168.50.1"', text)

    def test_port_forwards_emit_inline_ints(self):
        text = self._render(port_forwards=(
            PortForward(gateway_port=8888, host_port=51001),
        ))
        self.assertIn('- gateway_port: 8888', text)
        self.assertIn('host_port: 51001', text)
        self.assertIn('host: "127.0.0.1"', text)

    def test_empty_port_forwards_uses_empty_list_syntax(self):
        text = self._render(port_forwards=())
        self.assertIn("port_forwards: []", text)

    def test_no_tsi_or_allowlist_leak(self):
        # gvproxy's job is the explicit port-forward allowlist. No
        # mention of TSI primitives that the smolmachines research
        # note recommended and PRD 0023 explicitly rejected.
        text = self._render()
        for forbidden in ("allow-cidr", "allow-host", "TSI", "tsi"):
            self.assertNotIn(forbidden, text,
                             f"gvproxy config leaked {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
