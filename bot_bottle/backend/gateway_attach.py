"""Reattach running bottles to a freshly (re)started gateway (PRD 0081).

When the gateway or the orchestrator comes up cold, every bottle that was
already running loses something it depends on: the gateway minted a new CA
(firecracker; docker and macOS keep theirs on a host bind mount), the gateway's
per-bottle git-gate repos and credentials are gone, and the orchestrator's
in-memory egress tokens are empty. `reattach_running_bottles` restores all
three for every live bottle, the same way on every backend.

Backends supply only the primitives that differ, as a `GatewayAttacher`: how
to list live bottles, how to install a CA in one, and how to read one's
`ENV_VAR_SECRET`. What gets restored, which bottles are in scope, and what a
failure means all live here.

Failure policy: every bottle is attempted and every failure collected. By
default the failures then abort the bring-up, naming each bottle, because a
bottle that silently can't reach the gateway fails later and far from the
cause. `AttachPolicy.REPORT` reports them and continues instead, for a future
`--force` bring-up that must come up even with a wedged bottle (the
orchestrator is what you need running to inspect or remove it).
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass

from ..bottle_state import git_gate_state_dir
from ..gateway import Gateway, GatewayError
from ..git_gate.plan import GitGatePlan
from ..git_gate.provision import load_upstreams_snapshot
from ..git_gate.service import stage_git_gate_scripts
from ..log import info, warn
from ..orchestrator.client import OrchestratorClient, OrchestratorClientError
from .provision_gateway import GatewayTransport, provision_git_gate


class GatewayAttachError(RuntimeError):
    """One or more running bottles could not be reattached to the gateway."""


class AttachPolicy(enum.Enum):
    STRICT = "strict"  # any failure aborts the bring-up (default)
    REPORT = "report"  # failures are reported and the bring-up continues


@dataclass(frozen=True)
class RunningBottle:
    """A live bottle as the reattach flow sees it. `ref` is the backend's own
    handle (a container name, or a firecracker run dir); `slug` names its
    host-side state dir; `source_ip` is how the orchestrator knows it ("" while
    it has no address yet, e.g. mid-launch)."""

    ref: str
    slug: str
    source_ip: str


class GatewayAttacher(abc.ABC):
    """The backend-specific primitives of the reattach flow. None of them may
    run code from the agent's image as root with the agent's PATH (see
    `backend.util.ROOT_EXEC_PATH`)."""

    @abc.abstractmethod
    def running_bottles(self) -> list[RunningBottle]:
        """Every live bottle, including ones without an address yet. Raises
        if the live set can't be determined authoritatively (a partial list
        would silently skip bottles)."""

    @abc.abstractmethod
    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        """Make the bottle trust `ca_pem` (write it and rebuild the trust
        store). Raises on failure."""

    @abc.abstractmethod
    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        """The bottle's `ENV_VAR_SECRET`, or "" when it has none. Raises if it
        can't be determined."""


def reattach_running_bottles(
    attacher: GatewayAttacher,
    *,
    orchestrator_url: str,
    gateway: Gateway,
    policy: AttachPolicy = AttachPolicy.STRICT,
) -> None:
    """Restore the CA, git-gate state and egress tokens of every running
    bottle against the current gateway and orchestrator."""
    bottles = attacher.running_bottles()
    if not bottles:
        return
    client = OrchestratorClient(orchestrator_url)
    try:
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
            # No address yet, or not registered: a bottle mid-launch (it
            # registers once the pair is up) or mid-teardown. Either way there
            # is no registration to restore against.
            skipped.append(bottle.slug)
            continue
        try:
            attacher.install_ca(bottle, ca_pem)
            reprovision_git_gate(transport, bottle_id, bottle.slug)
            secret = attacher.read_env_var_secret(bottle)
            if not secret:
                raise GatewayAttachError("its ENV_VAR_SECRET is missing")
            # False means the orchestrator holds no secrets for this bottle:
            # nothing to restore.
            client.reprovision_gateway(bottle_id, secret)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Any step can fail in backend-specific ways (ssh, exec, HTTP);
            # each is reported against its bottle rather than aborting the rest.
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
        if policy is AttachPolicy.STRICT:
            raise GatewayAttachError(message)
        warn(message)


def reprovision_git_gate(transport: GatewayTransport, bottle_id: str, slug: str) -> None:
    """Re-provision one bottle's git-gate repos, credentials and hooks in the
    gateway, through the same `provision_git_gate` a launch uses. The upstream
    table and credentials come from the bottle's host-side state dir; the
    hooks, gitleaks config and entrypoint are rendered from the current code,
    as at launch, so a gateway rebuilt for a fix gets the fixed hooks. A bottle
    with no git upstreams has nothing to do."""
    state_dir = git_gate_state_dir(slug)
    upstreams = load_upstreams_snapshot(state_dir)
    if not upstreams:
        return
    entrypoint, hook, access_hook, gitleaks = stage_git_gate_scripts(state_dir, upstreams)
    provision_git_gate(transport, bottle_id, GitGatePlan(
        slug=slug,
        entrypoint_script=entrypoint,
        hook_script=hook,
        access_hook_script=access_hook,
        gitleaks_config=gitleaks,
        upstreams=upstreams,
    ))


__all__ = [
    "AttachPolicy",
    "GatewayAttachError",
    "GatewayAttacher",
    "RunningBottle",
    "reattach_running_bottles",
    "reprovision_git_gate",
]
