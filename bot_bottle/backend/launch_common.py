"""Launch steps every backend shares: git-gate deploy keys, orchestrator
registration, the gateway CA, and the agent's gateway URLs. Each backend's
`launch` keeps its own order.
"""

from __future__ import annotations

import dataclasses
from contextlib import ExitStack

from ..bottle_state import egress_state_dir, git_gate_state_dir
from ..contract.held_secrets import CANARY_KEY, HELD_ENV_PREFIX, MIN_HELD_SECRET_LEN
from ..contract.identity import GIT_HTTP_PORT
from ..contract.supervise import SUPERVISE_PORT
from ..egress import Egress, EgressPlan
from ..env import SECRET_KINDS, env_entry_kind
from ..git_gate import GitGate, GitGatePlan
from ..lib.log import warn
from .base import BottlePlan, BottleSpec


class LaunchTeardown:
    """Releases what a launch acquired: closes `stack`, then revokes the
    bottle's deploy keys either way. A close failure is reported with `label`
    and re-raised unless `reraise` is false. Safe to call more than once."""

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


def reuses_guest(plan: BottlePlan) -> bool:
    """Whether this launch starts the guest kept from the bottled agent's last
    run, if there is one: its bottle has `preserve_container`, and the launch
    didn't ask for a fresh one (`resume --fresh`)."""
    return plan.manifest.bottle.preserve_container and not plan.spec.fresh_guest


def keeps_guest(slug: str, preserve_container: bool) -> bool:
    """At teardown: whether to keep the guest (stop it, not remove it). Only for
    a bottle with `preserve_container` whose bottled agent is kept for resume;
    one that finished goes, with its home and state."""
    # pylint: disable-next=import-outside-toplevel
    from ..bottle_state import is_preserved
    return preserve_container and is_preserved(slug)


def provision_git_gate_keys(plan: BottlePlan) -> GitGatePlan:
    """Mint the bottle's gitea deploy keys, before its repos go into the
    gateway."""
    if not plan.git_gate_plan.upstreams:
        return plan.git_gate_plan
    return GitGate().provision_dynamic_keys(
        plan.manifest.bottle, plan.git_gate_plan, git_gate_state_dir(plan.slug),
        plan.secrets,
    )


def registration_tokens(plan: BottlePlan) -> dict[str, str]:
    """The map the bottle registers with the orchestrator (see
    `contract.held_secrets`): egress tokens for the gateway to inject, plus the
    secrets the bottle holds, which the gateway scans requests and pushes for."""
    tokens = Egress().resolve_token_values(
        plan.egress_plan.token_env_map, plan.egress_secrets,
    )
    return {**tokens, **held_secret_values(plan)}


def held_secret_values(plan: BottlePlan) -> dict[str, str]:
    """The secrets inside the bottle: its canary and its secret env values
    (`?`, `${VAR}` and `secret:NAME` alike), excluding values shorter than
    `MIN_HELD_SECRET_LEN`."""
    held: dict[str, str] = {}
    if plan.egress_plan.canary and plan.egress_plan.canary_env:
        held[CANARY_KEY] = plan.egress_plan.canary
    for name, raw in plan.manifest.bottle.env.items():
        value = plan.forwarded_env.get(name, "")
        if env_entry_kind(raw) in SECRET_KINDS and len(value) >= MIN_HELD_SECRET_LEN:
            held[HELD_ENV_PREFIX + name] = value
    return held


def stage_gateway_ca(plan: BottlePlan, ca_pem: str) -> EgressPlan:
    """Write the gateway CA to a stable host path for the provisioner and
    return the egress plan pointing at it."""
    ca_dir = egress_state_dir(plan.slug) / "gateway-ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    ca_file = ca_dir / "gateway-ca.pem"
    ca_file.write_text(ca_pem)
    return dataclasses.replace(
        plan.egress_plan,
        mitmproxy_ca_cert_only_host_path=ca_file,
    )


def gateway_agent_urls(
    plan: BottlePlan, gateway_host: str,
) -> tuple[str, str]:
    """The agent's git-gate and supervise URLs (supervise "" when the bottle
    doesn't use it). Both are in the agent's NO_PROXY. The gate's is set even
    for a bottle launched without repos: a repo granted while it runs (PRD
    0105) is reached through it."""
    git_gate_url = f"http://{gateway_host}:{GIT_HTTP_PORT}"
    supervise_url = (
        f"http://{gateway_host}:{SUPERVISE_PORT}/"
        if plan.supervise_plan is not None else ""
    )
    return git_gate_url, supervise_url


def terminal_title(spec: BottleSpec) -> str:
    """The agent terminal's title: the label, if any, with the agent name."""
    return f"{spec.label} ({spec.agent_name})" if spec.label else spec.agent_name
