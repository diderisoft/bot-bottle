"""Per-service control-plane signing keys (issue #476).

A `TrustDomain` is one service's signing material: its host-canonical key file,
the roles that key may sign, and the env vars its key and a pre-minted token ride
in. Scoping `mint`/`verify` to a domain's roles keeps one service's key from
signing (or accepting) another service's tokens.

Today there is one domain, `CONTROL_PLANE` — the orchestrator's key (roles
`{gateway, cli}`): the orchestrator holds it and mints the gateway's and CLI's
tokens. The host controller (#468) will add a **second** domain with its own key
the orchestrator never holds. That is the point: the host controller starts and
stops the orchestrator, so the orchestrator must not be able to mint the
credentials it uses to talk to it. Adding a `host` role to `CONTROL_PLANE`
instead would defeat that — the orchestrator holds that key, so it could forge
`host` tokens.

`ControlPlaneProvisioning` is the one seam every backend launcher uses to get the
orchestrator its key and the gateway its token, instead of re-deriving that
wiring per backend (the bug class behind PR #471 — see
`docs/prds/0079-control-plane-auth-provisioning.md`).

The domain descriptions (`TrustDomain`, `CONTROL_PLANE`) and token mint/verify
are in `contract.auth`; this module is the host half that reads the key files.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract.auth import CONTROL_PLANE, ROLE_GATEWAY, TrustDomain
from .paths import host_signing_key


class ProvisioningError(RuntimeError):
    """A control-plane auth invariant would be violated (e.g. starting the
    orchestrator without its signing key)."""


def signing_key(domain: TrustDomain) -> str:
    """`domain`'s host-canonical signing key (minted 0600 on first use).
    Host-side only; the value is injected into the owning process."""
    return host_signing_key(domain.key_filename)


def mint(domain: TrustDomain, role: str) -> str:
    """A role-scoped token for a delegate, signed with `domain`'s host key.
    Raises ValueError for a role the domain doesn't sign."""
    return domain.mint(role, signing_key(domain))


@dataclass(frozen=True)
class ControlPlaneProvisioning:
    """The one seam every backend launcher uses to provision control-plane auth,
    instead of re-deriving the four invariants that each cost a PR #471 review
    round: the orchestrator gets the raw key (`orchestrator_key`), the gateway
    gets a minted `gateway` token (`gateway_token`), the host CLI mints its own
    `cli` token from the same host-canonical key, and the orchestrator never
    starts open."""

    domain: TrustDomain = CONTROL_PLANE

    def orchestrator_key(self) -> str:
        """The raw signing key the orchestrator process must receive (carry it in
        `domain.key_env`). Fail-closed: raises rather than return "", since an
        empty key runs the server open — and being on a separate host does not
        stop the gateway from reaching the control plane (it must, for
        `/resolve`), so an open orchestrator would treat that gateway as `cli`."""
        key = signing_key(self.domain)
        if not key:
            raise ProvisioningError(
                f"refusing to start the {self.domain.name} orchestrator without "
                "a signing key: an open orchestrator authenticates no one and "
                "grants every caller that reaches it full `cli` (#476)"
            )
        return key

    def gateway_token(self) -> str:
        """The `gateway`-role token the gateway receives (carry it in
        `domain.token_env`) — minted from the key, never the key itself, so a
        compromised gateway cannot forge a `cli` token."""
        return mint(self.domain, ROLE_GATEWAY)


__all__ = [
    "ProvisioningError",
    "signing_key",
    "mint",
    "ControlPlaneProvisioning",
]
