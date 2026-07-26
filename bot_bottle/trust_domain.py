"""Trust domains: the unit of control-plane auth provisioning (issue #476).

A *trust domain* is one **credential boundary** — a single host-canonical
signing key plus the role set that key is allowed to sign, plus the env vars the
key and a pre-minted token are carried in. Provisioning is parameterized *per
domain*, not per key: the orchestrator control plane is one domain
(`CONTROL_PLANE` — key `orchestrator-token`, roles `{gateway, cli}`); a future
boundary (e.g. the host controller of #468) instantiates its **own** domain with
its **own** key, verifier, and role set rather than adding a role to this one.

That distinction is the security invariant behind the split: adding a `host`
role to the control plane's `ROLES` frozenset would let anything holding the
control-plane signing key (the orchestrator itself) mint host-controller tokens,
collapsing the boundary #468 needs — the host controller owns the orchestrator's
lifecycle, so it must not be forgeable *by* the orchestrator. Two keys, two
verifiers, two role sets.

`ControlPlaneProvisioning` is the single shared contract every backend launcher
satisfies instead of re-deriving, by hand, how to generate the signing key,
scope it to the orchestrator process, mint the gateway JWT, and keep the host
key canonical (the bug class that took PR #471 three review rounds — see
`docs/prds/prd-new-control-plane-auth-provisioning.md`).

Stdlib-only; the crypto lives in `orchestrator_auth` (untouched HMAC), the key
file lives in `paths` (no bot-bottle imports, safe to copy flat), and this module
composes the two into the provisioning seam.
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
    """One credential boundary: a host-canonical signing key + the role set it
    signs + the env vars its key and a pre-minted token ride in.

    `signing_key()` reads-or-mints the host-canonical key (never a guest's); the
    orchestrator process that *owns* the domain receives that raw key (via
    `key_env`), while a delegate (the data plane) receives only a pre-minted,
    role-scoped token (via `token_env`) it cannot rewrite. `mint`/`verify` are
    scoped to this domain's `roles`, so a token minted here neither carries nor
    verifies a role from another domain."""

    name: str
    key_filename: str
    roles: frozenset[str]
    key_env: str
    token_env: str

    def signing_key(self) -> str:
        """The host-canonical signing key for this domain (minted 0600 on first
        use). Host-side only — the value is injected into the owning process, not
        read there."""
        return host_signing_key(self.key_filename)

    def key_from_env(self, environ: Mapping[str, str] | None = None) -> str:
        """The signing key as seen by the *owning process* — read from `key_env`
        in the environment (default `os.environ`). "" when unset: the caller
        decides whether that is fatal (see `OrchestratorServer`'s open-mode
        fallback) or fail-closed (see `ControlPlaneProvisioning`)."""
        env = os.environ if environ is None else environ
        return env.get(self.key_env, "").strip()

    def mint(self, role: str) -> str:
        """A role-scoped token for a delegate, signed with this domain's key.
        Raises ValueError for a role outside this domain (mint only what this
        boundary accepts)."""
        if role not in self.roles:
            raise ValueError(f"role {role!r} is not in trust domain {self.name!r}")
        return orchestrator_auth.mint(role, self.signing_key(), roles=self.roles)

    def verify(self, token: str, key: str) -> str | None:
        """The role `token` carries under `key`, or None. `key` is passed
        explicitly (not read from the host file) because the verifier — the
        control-plane process — holds it in `key_env`, not on disk in its guest."""
        return orchestrator_auth.verify(token, key, roles=self.roles)


# The orchestrator control-plane domain: the signing key held by the
# orchestrator + host CLI, the `gateway` token handed to the data plane, and the
# `cli` token the CLI mints for itself.
CONTROL_PLANE = TrustDomain(
    name="control-plane",
    key_filename=ORCHESTRATOR_TOKEN_FILENAME,
    roles=orchestrator_auth.ROLES,
    key_env=ORCHESTRATOR_TOKEN_ENV,
    token_env=ORCHESTRATOR_AUTH_JWT_ENV,
)


@dataclass(frozen=True)
class Topology:
    """What a backend *is*, for control-plane auth provisioning (#476) — the
    backend declares this instead of encoding the provisioning decision by hand
    in its launcher.

    `data_plane_shares_control_host` — the data plane runs on the same host/VM as
    the control plane, so a reachable-but-OPEN control plane would hand that
    co-located data plane full `cli`. This is the default and the case that makes
    the signing key **mandatory** (open mode unreachable). Every current backend
    is co-located (docker/macOS: two containers on one host; firecracker: two VMs
    on one host, agents L3-isolated). A backend with a genuinely isolated control
    plane on a separate trusted host may declare False.

    `combined_guest` — control plane and data plane share a single guest (the
    retired combined infra VM). Informational today; kept so a future combined
    backend *declares* it rather than rediscovering the provisioning."""

    data_plane_shares_control_host: bool = True
    combined_guest: bool = False


# The default posture: data plane co-located with the control plane. Fail-closed
# — the signing key is mandatory. A backend opts out only by declaring otherwise.
COLOCATED = Topology(data_plane_shares_control_host=True)


@dataclass(frozen=True)
class ControlPlaneProvisioning:
    """The single shared control-plane auth provisioning contract (#476).

    A backend launcher satisfies THIS instead of re-deriving the four invariants
    that each took a PR #471 review round to get right:

      1. **Host-canonical key.** The signing key is `domain.signing_key()` — a
         guest is *handed* it, never generates or overwrites it (round 3's bug:
         the Firecracker guest clobbered the host key).
      2. **Split credential.** Only the orchestrator process gets the raw key
         (`orchestrator_key`); the data plane gets a pre-minted, role-scoped
         token (`gateway_token`) it cannot rewrite into `cli` (round 1's bug).
      3. **CLI validity across backends.** The host CLI mints its `cli` token
         from this same canonical key, so it stays valid no matter which backend
         (or how many) are co-running.
      4. **No open mode.** `orchestrator_key` fail-closes for any topology whose
         data plane shares the control plane's host/VM, so a launcher cannot
         start the control plane OPEN (round 2's bug)."""

    domain: TrustDomain = CONTROL_PLANE
    topology: Topology = field(default=COLOCATED)

    def orchestrator_key(self) -> str:
        """The raw signing key the control-plane *process* must receive (carry it
        in `domain.key_env`). Fail-closed: raises `ProvisioningError` rather than
        returning "" for a co-located topology, because an empty key makes the
        server run OPEN and hand the co-located data plane full `cli` (#476
        invariant 4)."""
        key = self.domain.signing_key()
        if not key and self.topology.data_plane_shares_control_host:
            raise ProvisioningError(
                f"refusing to provision the {self.domain.name} control plane "
                "without a signing key: its data plane shares this host/VM, so "
                "an OPEN control plane would grant that data plane full `cli` "
                "(#476)"
            )
        return key

    def gateway_token(self) -> str:
        """The pre-minted `gateway`-role token the data plane receives (carry it
        in `domain.token_env`) — minted from the canonical key, never the key
        itself, so a compromised data plane cannot forge a `cli` token."""
        return self.domain.mint(ROLE_GATEWAY)


__all__ = [
    "ProvisioningError",
    "TrustDomain",
    "CONTROL_PLANE",
    "Topology",
    "COLOCATED",
    "ControlPlaneProvisioning",
]
