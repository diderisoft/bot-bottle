"""Launch steps every backend shares.

All three backends launch a bottle onto the same per-host topology:
mint the bottle's git-gate deploy keys, register it with the orchestrator,
install the shared gateway's CA, and point the agent at the gateway's
git-gate and supervise endpoints. They differ in how the agent runs and how
it gets its address, so each backend's `launch` keeps its own order and calls
these helpers for the common steps.
"""

from __future__ import annotations

import dataclasses
from contextlib import ExitStack

from ..bottle_state import egress_state_dir, git_gate_state_dir
from ..contract.identity import GIT_HTTP_PORT
from ..contract.supervise import SUPERVISE_PORT
from ..egress import Egress, EgressPlan
from ..git_gate import GitGate, GitGatePlan
from ..lib.log import warn
from .base import BottlePlan, BottleSpec


class LaunchTeardown:
    """Releases what a launch acquired: closes `stack` (callbacks run in
    reverse order of registration), then revokes the bottle's git-gate deploy
    keys whether or not that succeeded.

    A failure while closing is reported with `label`, then re-raised unless
    `reraise` is false. Safe to call more than once: the bottle handle may
    call it on close and the launch context again on exit."""

    def __init__(self, plan: BottlePlan, label: str, *, reraise: bool = True) -> None:
        self.stack = ExitStack()
        self._label = label
        self._reraise = reraise
        self._bottle = plan.manifest.bottle
        self._git_gate_dir = git_gate_state_dir(plan.slug)
        self._secrets = plan.secrets

    def __call__(self) -> None:
        failure: BaseException | None = None
        try:
            self.stack.close()
        except BaseException as exc:  # noqa: W0718 - teardown must continue
            failure = exc
            warn(f"teardown failed for {self._label}: {exc!r}")
        GitGate().revoke_provisioned_keys(self._bottle, self._git_gate_dir, self._secrets)
        if failure is not None and self._reraise:
            raise failure


def provision_git_gate_keys(plan: BottlePlan) -> GitGatePlan:
    """Mint the bottle's dynamic (gitea) deploy keys, if it has upstreams.
    Must run before the bottle's repos are provisioned into the gateway."""
    if not plan.git_gate_plan.upstreams:
        return plan.git_gate_plan
    return GitGate().provision_dynamic_keys(
        plan.manifest.bottle, plan.git_gate_plan, git_gate_state_dir(plan.slug),
        plan.secrets,
    )


def egress_token_values(plan: BottlePlan) -> dict[str, str]:
    """The bottle's egress auth tokens, resolved from the plan's secrets and
    handed to the orchestrator in memory for the gateway to inject. The agent
    never sees them."""
    return Egress().resolve_token_values(
        plan.egress_plan.token_env_map, plan.egress_secrets,
    )


def stage_gateway_ca(plan: BottlePlan, ca_pem: str) -> EgressPlan:
    """Write the shared gateway's CA to a stable host path for the provisioner
    to install in the agent, and return the egress plan pointing at it. Every
    bottle on the host trusts this one CA."""
    ca_dir = egress_state_dir(plan.slug) / "gateway-ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    ca_file = ca_dir / "gateway-ca.pem"
    ca_file.write_text(ca_pem)
    return dataclasses.replace(
        plan.egress_plan,
        mitmproxy_ca_host_path=ca_file,
        mitmproxy_ca_cert_only_host_path=ca_file,
    )


def gateway_agent_urls(
    plan: BottlePlan, gateway_host: str, git_gate_plan: GitGatePlan,
) -> tuple[str, str]:
    """The agent's git-gate and supervise URLs on the shared gateway, or ""
    for whichever the bottle doesn't use. Both bypass the egress proxy: the
    agent's NO_PROXY covers `gateway_host`."""
    git_gate_url = (
        f"http://{gateway_host}:{GIT_HTTP_PORT}" if git_gate_plan.upstreams else ""
    )
    supervise_url = (
        f"http://{gateway_host}:{SUPERVISE_PORT}/"
        if plan.supervise_plan is not None else ""
    )
    return git_gate_url, supervise_url


def terminal_title(spec: BottleSpec) -> str:
    """The agent terminal's title: the label, if any, with the agent name."""
    return f"{spec.label} ({spec.agent_name})" if spec.label else spec.agent_name
