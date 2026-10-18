"""Reattach running bottles to a freshly (re)started gateway (PRD 0081).

When the gateway or the orchestrator comes up cold, every bottle that was
already running loses something it depends on: the gateway minted a new CA
(firecracker; docker and macOS keep theirs on a host bind mount), the gateway's
per-bottle git-gate repos and credentials are gone, and the orchestrator's
in-memory egress tokens are empty. `reattach_running_bottles` restores all
three for every live bottle, the same way on every backend.

Backends supply only the primitives that differ, as a `GatewayAttacher`: how
to list live bottles, how to install a CA in one, and how to read one's
`ENV_VAR_SECRET`. The flow and the failure policy live here.

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
import json
from dataclasses import dataclass
from pathlib import Path

from ..bottle_state import git_gate_state_dir
from ..gateway import Gateway, GatewayError
from ..gateway.git_gate.render import (
    GitGateUpstream,
    git_gate_render_access_hook,
    git_gate_render_entrypoint,
    git_gate_render_gitleaks_config,
    git_gate_render_hook,
)
from ..git_gate.plan import GitGatePlan
from ..log import info, warn
from ..orchestrator.client import OrchestratorClient, OrchestratorClientError
from ..orchestrator.reprovision import reprovision_bottles
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
    host-side state dir; `source_ip` is how the orchestrator knows it."""

    ref: str
    slug: str
    source_ip: str


class GatewayAttacher(abc.ABC):
    """The backend-specific primitives of the reattach flow."""

    @abc.abstractmethod
    def running_bottles(self) -> list[RunningBottle]:
        """Every live bottle. Raises if the live set can't be determined
        authoritatively (a partial list would silently skip bottles)."""

    @abc.abstractmethod
    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        """Make the bottle trust `ca_pem` (write it and rebuild the trust
        store). Raises on failure."""

    @abc.abstractmethod
    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        """The bottle's `ENV_VAR_SECRET`, or "" when it has none. Raises if it
        can't be read."""


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
    try:
        ca_pem = gateway.ca_cert_pem()
        registered = OrchestratorClient(orchestrator_url).list_bottles()
    except (GatewayError, OrchestratorClientError) as e:
        raise GatewayAttachError(f"could not reattach running bottles: {e}") from e
    bottle_ids: dict[str, str] = {}
    for record in registered:
        source_ip, bottle_id = record.get("source_ip"), record.get("bottle_id")
        if isinstance(source_ip, str) and isinstance(bottle_id, str):
            bottle_ids[source_ip] = bottle_id
    transport = gateway.provisioning_transport()
    failures: list[str] = []
    secrets: dict[str, str] = {}
    for bottle in bottles:
        bottle_id = bottle_ids.get(bottle.source_ip)
        if bottle_id is None:
            failures.append(
                f"{bottle.slug}: running but not registered with the orchestrator "
                f"(source IP {bottle.source_ip}); remove it with `bot-bottle cleanup`"
            )
            continue
        try:
            attacher.install_ca(bottle, ca_pem)
            reprovision_git_gate(transport, bottle_id, bottle.slug)
            secret = attacher.read_env_var_secret(bottle)
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Any step can fail in backend-specific ways (ssh, exec, cp); each
            # is reported against its bottle rather than aborting the rest.
            failures.append(f"{bottle.slug}: {e}")
            continue
        if secret:
            secrets[bottle.source_ip] = secret
    if secrets:
        try:
            reprovision_bottles(OrchestratorClient(orchestrator_url), secrets)
        except OrchestratorClientError as e:
            failures.append(f"restoring egress tokens failed: {e}")
    reattached = len(bottles) - len(failures)
    if reattached > 0:
        info(f"reattached {reattached} running bottle(s) to the gateway")
    if failures:
        message = (
            f"could not reattach {len(failures)} running bottle(s) to the gateway:\n  "
            + "\n  ".join(failures)
        )
        if policy is AttachPolicy.STRICT:
            raise GatewayAttachError(message)
        warn(message)


def reprovision_git_gate(transport: GatewayTransport, bottle_id: str, slug: str) -> None:
    """Re-provision one bottle's git-gate repos, credentials and hooks in the
    gateway, through the same `provision_git_gate` a launch uses. The upstream
    table and credentials come from the bottle's host-side state dir
    (`upstreams.json`, written at launch once dynamic deploy keys exist); the
    hooks, gitleaks config and entrypoint are rendered from the current code,
    never reused from the state dir, so a gateway rebuilt for a fix gets the
    fixed hooks. A bottle with no git upstreams has no snapshot and nothing to
    do."""
    state_dir = git_gate_state_dir(slug)
    snapshot = state_dir / "upstreams.json"
    if not snapshot.is_file():
        return
    upstreams: list[GitGateUpstream] = []
    for u in json.loads(snapshot.read_text()):
        name = u["name"]
        # A gitea deploy key lives in the state dir; a static key keeps its
        # manifest path.
        key_in_state = state_dir / f"{name}-key"
        known_hosts = state_dir / f"{name}-known_hosts"
        upstreams.append(GitGateUpstream(
            name=name,
            upstream_url=u["upstream_url"],
            upstream_host=u.get("upstream_host", ""),
            upstream_port=u.get("upstream_port", ""),
            identity_file=(
                str(key_in_state) if key_in_state.is_file() else u.get("identity_file", "")
            ),
            known_host_key=u.get("known_host_key", ""),
            known_hosts_file=known_hosts if known_hosts.is_file() else Path(),
        ))
    if not upstreams:
        return
    rendered = {
        "git_gate_entrypoint.sh": (git_gate_render_entrypoint(tuple(upstreams)), 0o600),
        "git_gate_pre_receive.sh": (git_gate_render_hook(), 0o600),
        # git daemon execs the access hook directly, so it needs the x bit.
        "git_gate_access_hook.sh": (git_gate_render_access_hook(), 0o700),
        "git_gate_gitleaks.toml": (git_gate_render_gitleaks_config(), 0o600),
    }
    for name, (text, mode) in rendered.items():
        (state_dir / name).write_text(text)
        (state_dir / name).chmod(mode)
    provision_git_gate(transport, bottle_id, GitGatePlan(
        slug=slug,
        entrypoint_script=state_dir / "git_gate_entrypoint.sh",
        hook_script=state_dir / "git_gate_pre_receive.sh",
        access_hook_script=state_dir / "git_gate_access_hook.sh",
        gitleaks_config=state_dir / "git_gate_gitleaks.toml",
        upstreams=tuple(upstreams),
    ))


__all__ = [
    "AttachPolicy",
    "GatewayAttachError",
    "GatewayAttacher",
    "RunningBottle",
    "reattach_running_bottles",
    "reprovision_git_gate",
]
