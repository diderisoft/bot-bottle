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
`docs/prds/prd-new-control-plane-auth-provisioning.md`).

Stdlib-only: the HMAC lives in `orchestrator_auth`, the key file in `paths`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import orchestrator_auth
from .orchestrator_auth import ROLE_GATEWAY
from .paths import (
    ORCHESTRATOR_AUTH_JWT_ENV,
    ORCHESTRATOR_TOKEN_ENV,
    ORCHESTRATOR_TOKEN_FILENAME,
    host_signing_key,
)


class ProvisioningError(RuntimeError):
    """A control-plane auth invariant would be violated (e.g. starting a
    co-located control plane without its signing key — which would run OPEN)."""


@dataclass(frozen=True)
class TrustDomain:
    """One service's signing material: a host-canonical key file, the roles that
    key may sign, and the env vars its key and a minted token ride in.

    The service that *owns* the domain (e.g. the orchestrator) receives the raw
    key via `key_env`; a delegate (e.g. the gateway) receives only a pre-minted,
    role-scoped token via `token_env` it cannot rewrite. `mint`/`verify` are
    scoped to `roles`, so this service's key can neither sign nor accept another
    service's role."""

    name: str
    key_filename: str
    roles: frozenset[str]
    key_env: str
    token_env: str

    def signing_key(self) -> str:
        """This service's host-canonical signing key (minted 0600 on first use).
        Host-side only — the value is injected into the owning process."""
        return host_signing_key(self.key_filename)

    def key_from_env(self, environ: Mapping[str, str] | None = None) -> str:
        """The signing key as the owning process sees it — read from `key_env`
        (default `os.environ`). "" when unset; the caller decides whether that is
        fatal (`ControlPlaneProvisioning`) or the open-mode fallback
        (`OrchestratorServer`)."""
        env = os.environ if environ is None else environ
        return env.get(self.key_env, "").strip()

    def mint(self, role: str) -> str:
        """A role-scoped token for a delegate, signed with this service's key.
        Raises ValueError for a role this service doesn't sign."""
        if role not in self.roles:
            raise ValueError(f"role {role!r} is not in trust domain {self.name!r}")
        return orchestrator_auth.mint(role, self.signing_key(), roles=self.roles)

    def verify(self, token: str, key: str) -> str | None:
        """The role `token` carries under `key`, or None. `key` is passed in
        (not read from disk) because the verifier — the control-plane process —
        holds it in `key_env`, not on disk in its guest."""
        return orchestrator_auth.verify(token, key, roles=self.roles)


# The orchestrator's domain: the key the orchestrator (and host CLI) holds, the
# `gateway` token it mints for the data plane, and the `cli` token the CLI mints
# for itself. #468's host controller will add a second, separate domain.
CONTROL_PLANE = TrustDomain(
    name="control-plane",
    key_filename=ORCHESTRATOR_TOKEN_FILENAME,
    roles=orchestrator_auth.ROLES,
    key_env=ORCHESTRATOR_TOKEN_ENV,
    token_env=ORCHESTRATOR_AUTH_JWT_ENV,
)


@dataclass(frozen=True)
class Topology:
    """Where a backend runs the two planes, so the provisioning seam can decide
    whether an open control plane is dangerous — the backend declares this
    instead of hardcoding the decision in its launcher.

    `data_plane_shares_control_host` — the gateway runs on the same host/VM as
    the orchestrator, so an open orchestrator would hand the co-located gateway
    full `cli`. The default, and what makes the signing key mandatory. Every
    current backend is co-located (docker/macOS: two containers on one host;
    firecracker: two VMs on one host, agents L3-isolated); an isolated control
    plane on a separate trusted host may declare False.

    `combined_guest` — both planes in one guest (the retired combined infra VM).
    Informational; kept so a future combined backend declares it."""

    data_plane_shares_control_host: bool = True
    combined_guest: bool = False


# The default posture: data plane co-located with the control plane. Fail-closed
# — the signing key is mandatory. A backend opts out only by declaring otherwise.
COLOCATED = Topology(data_plane_shares_control_host=True)


@dataclass(frozen=True)
class ControlPlaneProvisioning:
    """The one seam every backend launcher uses to provision control-plane auth,
    instead of re-deriving the four invariants that each cost a PR #471 review
    round: the orchestrator gets the raw key (`orchestrator_key`), the gateway
    gets a minted `gateway` token (`gateway_token`), the host CLI mints its own
    `cli` token from the same host-canonical key, and the orchestrator never
    starts open where its gateway is co-located."""

    domain: TrustDomain = CONTROL_PLANE
    topology: Topology = field(default=COLOCATED)

    def orchestrator_key(self) -> str:
        """The raw signing key the orchestrator process must receive (carry it in
        `domain.key_env`). Fail-closed: raises rather than return "" for a
        co-located topology, since an empty key runs the server open and hands
        the co-located gateway full `cli`."""
        key = self.domain.signing_key()
        if not key and self.topology.data_plane_shares_control_host:
            raise ProvisioningError(
                f"refusing to start the {self.domain.name} orchestrator without "
                "a signing key: its gateway shares this host/VM, so an open "
                "orchestrator would grant that gateway full `cli` (#476)"
            )
        return key

    def gateway_token(self) -> str:
        """The `gateway`-role token the gateway receives (carry it in
        `domain.token_env`) — minted from the key, never the key itself, so a
        compromised gateway cannot forge a `cli` token."""
        return self.domain.mint(ROLE_GATEWAY)


__all__ = [
    "ProvisioningError",
    "TrustDomain",
    "CONTROL_PLANE",
    "Topology",
    "COLOCATED",
    "ControlPlaneProvisioning",
]
