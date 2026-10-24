"""Host side of the control-plane signing keys: reading each `TrustDomain`'s
key file, minting tokens, and what each service is handed.

`ControlPlaneProvisioning` gives the orchestrator its key and the gateway a
`gateway` token. `HostControlProvisioning` gives an orchestrator an
`orchestrator` token for the host controller, never the `HOST_CONTROL` key, so
it can't mint a `cli` token for the daemon that starts and stops it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .contract.auth import CONTROL_PLANE, ROLE_GATEWAY, TrustDomain
from .contract.hostctl import GUEST_PORT, HOST_CONTROL, HOSTCTL_URL_ENV, ROLE_ORCHESTRATOR
from .paths import host_signing_key


class ProvisioningError(RuntimeError):
    """A control-plane auth invariant would be violated, e.g. starting the
    orchestrator without its key."""


def signing_key(domain: TrustDomain) -> str:
    """`domain`'s signing key, created 0600 on first use."""
    return host_signing_key(domain.key_filename)


def mint(domain: TrustDomain, role: str) -> str:
    """A role-scoped token signed with `domain`'s key. Raises ValueError for a
    role the domain doesn't sign."""
    return domain.mint(role, signing_key(domain))


@dataclass(frozen=True)
class ControlPlaneProvisioning:
    """Control-plane auth for a launcher: the orchestrator gets the raw key,
    the gateway a minted `gateway` token, the host CLI mints its own `cli`
    token, and the orchestrator never starts open."""

    domain: TrustDomain = CONTROL_PLANE

    def orchestrator_key(self) -> str:
        """The key the orchestrator receives in `domain.key_env`. Raises rather
        than return "", since an empty key would serve the control plane open."""
        key = signing_key(self.domain)
        if not key:
            raise ProvisioningError(
                f"refusing to start the {self.domain.name} orchestrator without "
                "a signing key: an open orchestrator authenticates no one and "
                "grants every caller that reaches it full `cli`"
            )
        return key

    def gateway_token(self) -> str:
        """The `gateway` token for `domain.token_env`, never the key, so a
        compromised gateway can't forge a `cli` token."""
        return mint(self.domain, ROLE_GATEWAY)


@dataclass(frozen=True)
class HostLink:
    """The host's address on an orchestrator's network and that network's CIDR:
    where the host controller listens and the only peers it accepts. `backend`
    is whose bottles that listener reports as live."""

    address: str
    network: str
    backend: str = ""


@dataclass(frozen=True)
class HostControlProvisioning:
    """What the host controller hands an orchestrator it brings up: its URL on
    the orchestrator's link and an `orchestrator` token. `listen` serves a link
    (idempotent)."""

    listen: Callable[[HostLink], None]
    port: int = GUEST_PORT
    domain: TrustDomain = HOST_CONTROL

    def url(self, link: HostLink) -> str:
        """The daemon's URL from `link`."""
        return f"http://{link.address}:{self.port}"

    def orchestrator_env(self, link: HostLink) -> dict[str, str]:
        """The orchestrator's env: the URL in `HOSTCTL_URL_ENV` and its token
        in `domain.token_env`. Starts serving `link`."""
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
