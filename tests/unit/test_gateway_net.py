"""Unit: shared-gateway source-IP allocation (PRD 0070)."""

from __future__ import annotations

import unittest

from bot_bottle.backend.docker.gateway_net import NoFreeAddressError, next_free_ip


class TestNextFreeIp(unittest.TestCase):
    def test_skips_router_and_returns_first_host(self) -> None:
        # .1 is docker's router; the first assignable address is .2.
        self.assertEqual("172.18.0.2", next_free_ip("172.18.0.0/16", []))

    def test_skips_taken_addresses(self) -> None:
        # Gateway container holds .2, a live bottle holds .3 -> next is .4.
        self.assertEqual(
            "172.18.0.4", next_free_ip("172.18.0.0/16", ["172.18.0.2", "172.18.0.3"]),
        )

    def test_taken_order_does_not_matter(self) -> None:
        self.assertEqual(
            "172.18.0.2", next_free_ip("172.18.0.0/16", ["172.18.0.3", "172.18.0.5"]),
        )

    def test_allocation_is_deterministic(self) -> None:
        taken = ["172.18.0.2"]
        self.assertEqual(next_free_ip("172.18.0.0/16", taken),
                         next_free_ip("172.18.0.0/16", taken))

    def test_raises_when_subnet_exhausted(self) -> None:
        # /30: hosts are .1 (router, reserved) and .2; taking .2 leaves none.
        with self.assertRaises(NoFreeAddressError):
            next_free_ip("10.9.9.0/30", ["10.9.9.2"])

    def test_accepts_host_bits_set_cidr(self) -> None:
        # A container's inspected address arrives as e.g. 172.18.0.2/16.
        self.assertEqual("172.18.0.2", next_free_ip("172.18.0.5/16", []))


if __name__ == "__main__":
    unittest.main()
