"""Unit: trust domains + the shared control-plane provisioning contract (#476)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.contract import auth as orchestrator_auth
from bot_bottle.contract.auth import CONTROL_PLANE, ROLE_CLI, ROLE_GATEWAY, TrustDomain
from bot_bottle.trust_domain import (
    ControlPlaneProvisioning,
    ProvisioningError,
    mint,
)

# A second, unrelated domain — the shape #468's host controller would take: its
# own key file, its own role, its own env vars. Distinct from CONTROL_PLANE.
_HOST_CTRL = TrustDomain(
    name="host-controller",
    key_filename="host-controller-token",
    roles=frozenset({"host"}),
    key_env="BOT_BOTTLE_HOST_CONTROLLER_TOKEN",
    token_env="BOT_BOTTLE_HOST_CONTROLLER_JWT",
)


class TestTrustDomainMintVerify(unittest.TestCase):
    def test_mint_verify_round_trips_within_a_domain(self) -> None:
        with patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            tok = mint(CONTROL_PLANE, ROLE_GATEWAY)
        self.assertEqual(ROLE_GATEWAY, CONTROL_PLANE.verify(tok, "k"))

    def test_mint_rejects_a_role_outside_the_domain(self) -> None:
        # `host` is a valid role in _HOST_CTRL but not in the control plane —
        # the control-plane key must refuse to mint it.
        with patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            with self.assertRaises(ValueError):
                mint(CONTROL_PLANE, "host")

    def test_a_custom_role_set_verifies_its_own_role(self) -> None:
        with patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            tok = mint(_HOST_CTRL, "host")
        self.assertEqual("host", _HOST_CTRL.verify(tok, "k"))

    def test_key_from_env_reads_the_domains_env_var(self) -> None:
        self.assertEqual(
            "secret", CONTROL_PLANE.key_from_env({CONTROL_PLANE.key_env: " secret "}))
        self.assertEqual("", CONTROL_PLANE.key_from_env({}))


class TestDomainBoundary(unittest.TestCase):
    """The #476/#468 invariant: two domains, two keys, two role sets — one
    domain's key can neither mint nor verify the other's tokens."""

    def test_a_control_plane_token_does_not_verify_under_another_domains_key(
        self,
    ) -> None:
        # Even with an IDENTICAL underlying key, a `cli` token minted under the
        # control-plane domain must not verify as a role in the host-controller
        # domain — the role isn't in that domain's set.
        cli_tok = orchestrator_auth.mint(ROLE_CLI, "shared-bytes")
        self.assertIsNone(_HOST_CTRL.verify(cli_tok, "shared-bytes"))

    def test_distinct_keys_do_not_cross_verify(self) -> None:
        # The realistic case: distinct host-canonical keys per domain. A token
        # signed by one key never verifies under the other.
        host_tok = orchestrator_auth.mint(
            "host", "host-ctrl-key", roles=_HOST_CTRL.roles)
        self.assertIsNone(_HOST_CTRL.verify(host_tok, "control-plane-key"))
        self.assertEqual("host", _HOST_CTRL.verify(host_tok, "host-ctrl-key"))


class TestControlPlaneProvisioning(unittest.TestCase):
    def test_orchestrator_key_returns_the_canonical_key(self) -> None:
        prov = ControlPlaneProvisioning()
        with patch("bot_bottle.trust_domain.host_signing_key", return_value="key"):
            self.assertEqual("key", prov.orchestrator_key())

    def test_orchestrator_key_fail_closes_when_empty(self) -> None:
        # Invariant 4: the orchestrator must never start without a key. There is no
        # topology opt-out: a separate host does not stop the gateway (or any
        # other caller) from reaching the control-plane listener.
        prov = ControlPlaneProvisioning()
        with patch("bot_bottle.trust_domain.host_signing_key", return_value=""):
            with self.assertRaises(ProvisioningError):
                prov.orchestrator_key()

    def test_gateway_token_is_a_verifiable_gateway_role_token(self) -> None:
        prov = ControlPlaneProvisioning()
        with patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            tok = prov.gateway_token()
        self.assertEqual(ROLE_GATEWAY, CONTROL_PLANE.verify(tok, "k"))

    def test_gateway_token_cannot_be_reused_as_cli(self) -> None:
        # The data plane's token is `gateway`-scoped: it never carries `cli`.
        prov = ControlPlaneProvisioning()
        with patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            tok = prov.gateway_token()
        self.assertNotEqual(ROLE_CLI, CONTROL_PLANE.verify(tok, "k"))


if __name__ == "__main__":
    unittest.main()
