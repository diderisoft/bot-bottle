"""Reattach running bottles to a freshly (re)started gateway.

A cold infra pair loses what running bottles depend on: the gateway's address
may change, its git-gate repos and credentials are gone, the orchestrator's
egress tokens are empty, and after a CA rotation the bottles distrust the
gateway. `reattach_running_bottles` restores all four. Backends supply the
primitives as a `GatewayAttacher`.

Every bottle is attempted and failures collected; then they abort the
bring-up, naming each bottle.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from ..bottle_state import git_gate_state_dir
from ..control.gateway import Gateway, GatewayError
from ..git_gate.plan import GitGatePlan
from ..git_gate.provision import load_upstreams_snapshot
from ..git_gate.service import load_trailer_settings, stage_git_gate_scripts
from ..lib.log import info
from ..control.client import OrchestratorClient, OrchestratorClientError
from .provision_gateway import GatewayTransport, provision_git_gate


class GatewayAttachError(RuntimeError):
    """One or more running bottles could not be reattached to the gateway."""


@dataclass(frozen=True)
class RunningBottle:
    """A live bottle for reattach. `ref` is the backend's handle (container
    name or firecracker run dir); `slug` names its state dir; `source_ip` is ""
    until it has an address."""

    ref: str
    slug: str
    source_ip: str


class GatewayAttacher(abc.ABC):
    """Backend primitives for reattach. None may run the agent image's code as
    root with the agent's PATH (see `backend.util.ROOT_EXEC_PATH`)."""

    @abc.abstractmethod
    def running_bottles(self) -> list[RunningBottle]:
        """Every live bottle, including ones without an address. Raises if the
        set can't be determined authoritatively."""

    @abc.abstractmethod
    def point_at_gateway(self, bottle: RunningBottle, address: str) -> None:
        """Point the bottle at the gateway's current `address`, or confirm it
        where the address is fixed. Raises if the bottle can't reach it."""

    @abc.abstractmethod
    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        """Make the bottle trust `ca_pem`. Raises on failure."""

    @abc.abstractmethod
    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        """The bottle's `ENV_VAR_SECRET`, or "". Raises if it can't be read."""


def reattach_running_bottles(
    attacher: GatewayAttacher,
    *,
    orchestrator_url: str,
    gateway: Gateway,
) -> None:
    """Restore every running bottle's gateway address, CA, git-gate state and
    egress tokens."""
    bottles = attacher.running_bottles()
    if not bottles:
        return
    client = OrchestratorClient(orchestrator_url)
    try:
        address = gateway.address()
        ca_pem = gateway.ca_cert_pem()
        registered = client.list_bottles()
    except (GatewayError, OrchestratorClientError) as e:
        raise GatewayAttachError(f"could not reattach running bottles: {e}") from e
    bottle_ids: dict[str, str] = {}
    for record in registered:
        source_ip, bottle_id = record.get("source_ip"), record.get("bottle_id")
        if isinstance(source_ip, str) and isinstance(bottle_id, str):
            bottle_ids[source_ip] = bottle_id
    transport = gateway.provisioning_transport()
    failures: list[str] = []
    skipped: list[str] = []
    reattached = 0
    for bottle in bottles:
        bottle_id = bottle_ids.get(bottle.source_ip) if bottle.source_ip else None
        if bottle_id is None:
            # Mid-launch or mid-teardown: nothing registered to restore.
            skipped.append(bottle.slug)
            continue
        try:
            attacher.point_at_gateway(bottle, address)
            attacher.install_ca(bottle, ca_pem)
            reprovision_git_gate(transport, bottle_id, bottle.slug)
            secret = attacher.read_env_var_secret(bottle)
            if not secret:
                raise GatewayAttachError("its ENV_VAR_SECRET is missing")
            # False: the orchestrator holds no secrets for this bottle.
            client.reprovision_gateway(bottle_id, secret)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Report each failure against its bottle and carry on.
            failures.append(f"{bottle.slug}: {e}")
            continue
        reattached += 1
    if reattached:
        info(f"reattached {reattached} running bottle(s) to the gateway")
    if skipped:
        info(f"skipped {len(skipped)} bottle(s) not registered yet: {', '.join(skipped)}")
    if failures:
        message = (
            f"could not reattach {len(failures)} running bottle(s) to the gateway:\n  "
            + "\n  ".join(failures)
            + "\nStop or remove those bottles, then retry."
        )
        raise GatewayAttachError(message)


def reprovision_git_gate(transport: GatewayTransport, bottle_id: str, slug: str) -> None:
    """Re-provision one bottle's git-gate repos, credentials and hooks, as a
    launch does. Upstreams and credentials come from its state dir; hooks are
    rendered from the current code."""
    state_dir = git_gate_state_dir(slug)
    upstreams = load_upstreams_snapshot(state_dir)
    if not upstreams:
        return
    hook, access_hook, gitleaks = stage_git_gate_scripts(state_dir)
    trailer, trailer_mode = load_trailer_settings(state_dir)
    provision_git_gate(transport, bottle_id, GitGatePlan(
        slug=slug,
        hook_script=hook,
        access_hook_script=access_hook,
        gitleaks_config=gitleaks,
        upstreams=upstreams,
        trailer=trailer,
        trailer_mode=trailer_mode,
    ))


__all__ = [
    "GatewayAttachError",
    "GatewayAttacher",
    "RunningBottle",
    "reattach_running_bottles",
    "reprovision_git_gate",
]
