"""End-to-end launch flow for the smolmachines backend
(PRD 0023 chunk 2d).

Brings up the per-bottle docker bridge + sidecar bundle, creates
+ starts the smolvm guest pointed at the bundle's pinned IP via
the Smolfile's TSI allowlist, yields a `SmolmachinesBottle`
handle, tears everything down on context exit.

Chunk-2d scope: smoke-test plumbing for the launch + exec round
trip. The bundle daemons aren't supplied with config files yet
(pipelock.yaml, routes.yaml, etc.); the bundle's init supervisor
exits cleanly when nothing is configured. Real provisioning + CA
install + the inner Plan plumbing land in chunk 4."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Callable, Generator

from . import smolvm as _smolvm
from . import sidecar_bundle as _bundle
from .bottle import SmolmachinesBottle
from .bottle_plan import SmolmachinesBottlePlan


@contextmanager
def launch(
    plan: SmolmachinesBottlePlan,
    *,
    provision: Callable[[SmolmachinesBottlePlan, str], str | None],
) -> Generator[SmolmachinesBottle, None, None]:
    """Build + run the bottle and yield a handle; tear everything
    down on exit. Errors during bringup unwind any partial state
    via the ExitStack."""
    stack = ExitStack()
    try:
        # 1. Per-bottle docker bridge + bundle container.
        network = _bundle.bundle_network_name(plan.slug)
        _bundle.create_bundle_network(network, plan.bundle_subnet, plan.bundle_gateway)
        stack.callback(_bundle.remove_bundle_network, network)

        bundle_spec = _bundle.BundleLaunchSpec(
            slug=plan.slug,
            network_name=network,
            subnet=plan.bundle_subnet,
            gateway=plan.bundle_gateway,
            bundle_ip=plan.bundle_ip,
            # Chunk 2d: empty daemon set — the init supervisor
            # logs "no daemons selected" and idles. Real daemon
            # bringup with inner-Plan-driven env + volumes lands
            # in chunk 4 alongside provisioning.
            daemons_csv="",
            # PRD 0023 chunk 3: pin egress to localhost INSIDE the
            # bundle so the agent's TSI-permitted `<bundle-ip>:*`
            # connect to :9099 refuses at the socket level. Always
            # set in smolmachines mode — agent dials pipelock, not
            # egress, so egress is bundle-internal regardless of
            # whether routes are declared. The docker backend
            # doesn't set this env (egress on 0.0.0.0 by default)
            # since the docker agent goes via the egress alias.
            environment=("EGRESS_LISTEN_HOST=127.0.0.1",),
        )
        _bundle.start_bundle(bundle_spec)
        stack.callback(_bundle.stop_bundle, plan.slug)

        # 2. smolvm VM. --from carries the pre-packed
        # .smolmachine artifact (built by prepare); --allow-cidr
        # + -e carry the per-bottle TSI allowlist + env. Smolfile
        # isn't usable here — smolvm 0.8.0 makes `--from` and
        # `--smolfile` mutually exclusive.
        _smolvm.machine_create(
            plan.machine_name,
            from_path=plan.agent_from_path,
            allow_cidrs=[f"{plan.bundle_ip}/32"],
            env=plan.guest_env,
        )
        stack.callback(_smolvm.machine_delete, plan.machine_name)
        _smolvm.machine_start(plan.machine_name)
        stack.callback(_smolvm.machine_stop, plan.machine_name)

        # 3. Provision (CA / prompt / skills / git / supervise).
        # The orchestrator runs each one in order; provision_*
        # methods left as stubs (chunk 4 follow-ons) are no-ops.
        prompt_path = provision(plan, plan.machine_name)

        # 4. Yield the handle. The prompt_path drives whether
        # exec_claude adds --append-system-prompt-file to claude's
        # argv (None → no flag).
        yield SmolmachinesBottle(plan.machine_name, prompt_path=prompt_path)
    finally:
        stack.close()
