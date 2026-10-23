"""Per-service control-plane signing keys.

A `TrustDomain` is one service's signing material: its host-canonical key file,
the roles that key may sign, and the env vars its key and a pre-minted token ride
in. Scoping `mint`/`verify` to a domain's roles keeps one service's key from
signing (or accepting) another service's tokens.

Today there is one domain, `CONTROL_PLANE` — the orchestrator's key (roles
`{gateway, cli}`): the orchestrator holds it and mints the gateway's and CLI's
tokens. The host controller will add a **second** domain with its own key
the orchestrator never holds. That is the point: the host controller starts and
stops the orchestrator, so the orchestrator must not be able to mint the
credentials it uses to talk to it. Adding a `host` role to `CONTROL_PLANE`
instead would defeat that — the orchestrator holds that key, so it could forge
`host` tokens.

`ControlPlaneProvisioning` is the one seam every backend launcher uses to get the
orchestrator its key and the gateway its token, instead of re-deriving that
wiring per backend (see
`docs/prds/0079-control-plane-auth-provisioning.md`).

The domain descriptions (`TrustDomain`, `CONTROL_PLANE`) and token mint/verify
are in `contract.auth`; this module is the host half that reads the key files.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .contract.auth import CONTROL_PLANE, ROLE_GATEWAY, TrustDomain
from .contract.hostctl import GUEST_PORT, HOST_CONTROL, HOSTCTL_URL_ENV, ROLE_ORCHESTRATOR
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
    instead of re-deriving its four invariants: the orchestrator gets the raw
    key (`orchestrator_key`), the gateway gets a minted `gateway` token (`gateway_token`), the host CLI mints its own
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
                "grants every caller that reaches it full `cli`"
            )
        return key

    def gateway_token(self) -> str:
        """The `gateway`-role token the gateway receives (carry it in
        `domain.token_env`) — minted from the key, never the key itself, so a
        compromised gateway cannot forge a `cli` token."""
        return mint(self.domain, ROLE_GATEWAY)


@dataclass(frozen=True)
class HostLink:
    """The host's address on an orchestrator's network, and that network
    (CIDR): where the host controller listens for the orchestrator, and the
    only peers it accepts there. `backend` names the orchestrator's backend,
    whose bottles that listener reports as live."""

    address: str
    network: str
    backend: str = ""


@dataclass(frozen=True)
class HostControlProvisioning:
    """What the host controller hands an orchestrator it brings up:
    the daemon's URL on the orchestrator's link and an `orchestrator`-role
    token. Never the `HOST_CONTROL` key, so the orchestrator can't mint itself
    a `cli` token. `listen` has the daemon serve a link (idempotent)."""

    listen: Callable[[HostLink], None]
    port: int = GUEST_PORT
    domain: TrustDomain = HOST_CONTROL

    def url(self, link: HostLink) -> str:
        """The daemon's URL as the orchestrator on `link` reaches it."""
        return f"http://{link.address}:{self.port}"

    def orchestrator_env(self, link: HostLink) -> dict[str, str]:
        """The env the orchestrator process gets: the URL in `HOSTCTL_URL_ENV`
        and its token in `domain.token_env`. Starts serving `link`."""
        self.listen(link)
        return {HOSTCTL_URL_ENV: self.url(link),
                self.domain.token_env: mint(self.domain, ROLE_ORCHESTRATOR)}


__all__ = [
    "HostControlProvisioning",
    "HostLink",
    "ProvisioningError",
    "signing_key",
    "mint",
    "ControlPlaneProvisioning",
]
