"""Firecracker launch.

Resolve the agent base (cached, published, built in the infra VM, or a
committed snapshot), ensure the infra VMs, claim a TAP slot, register by guest
IP and provision git-gate, boot the VM, run the isolation probe, and provision
over SSH.

The VM sends gateway traffic to its TAP's host IP; the netpool DNATs it to the
gateway VM. The nft table (checked in preflight) lets the VM reach nothing
else, including the orchestrator.
"""

from __future__ import annotations

import dataclasses
import shlex
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generator

from ... import agent_image, agent_overlay
from ...agent_provider import runtime_for
from ...published_artifacts import FORMAT_EXT4, FORMAT_OCI, Artifact, ArtifactsManifestError
from ...bottle_state import committed_rootfs_path, read_committed_image
from ...egress import Egress
from ...image_cache import check_stale_path
from ...contract.hostctl import Settings
from ...lib.log import die, info
from ..launch_common import (
    LaunchTeardown,
    registration_tokens,
    gateway_agent_urls,
    provision_git_gate_keys,
    stage_gateway_ca,
    terminal_title,
)
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from . import firecracker_vm, image_builder, isolation_probe, lifecycle_lock, netpool, util
from .bottle import FirecrackerBottle
from .bottle_plan import FirecrackerBottlePlan
from ...orchestrator.store.config_store import resolve_teardown_timeout
from ...paths import host_db_path
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from .infra import FirecrackerInfraService
from .consolidated_launch import (
    launch_consolidated,
    persist_env_var_secret,
    deprovision_consolidated,
)



if TYPE_CHECKING:
    from ..adoption import AdoptionRecord


@contextmanager
def adopted(
    bottle_id: str, record: "AdoptionRecord", settings: Settings,
) -> Generator[FirecrackerBottle, None, None]:
    """A bottle a previous host controller launched: exec into it over SSH,
    then tear it down as its holder would have (stop the VM, remove its run
    dir, deregister)."""
    from .cleanup import stop_vmms  # pylint: disable=import-outside-toplevel
    run_dir = util.cache_dir() / "run" / bottle_id
    try:
        yield FirecrackerBottle(record.container, private_key=run_dir / "bottle_id_ed25519",
                                guest_ip=firecracker_vm.guest_ip_of(run_dir))
    finally:
        stop_vmms(run_dir)
        shutil.rmtree(run_dir, ignore_errors=True)
        if record.registry_id:
            deprovision_consolidated(record.registry_id, service=FirecrackerInfraService(settings),
                                     timeout=resolve_teardown_timeout(host_db_path()))

@contextmanager
def launch(
    plan: FirecrackerBottlePlan,
    agent_base: Path,
    *,
    provision: Callable[[FirecrackerBottlePlan, "FirecrackerBottle"], str | None],
) -> Generator[FirecrackerBottle, None, None]:
    """Launch and provision a firecracker bottle. Tears down on exit."""
    teardown = LaunchTeardown(plan, "firecracker")
    stack = teardown.stack

    try:
        # The agent base was resolved in `BottleBackend.launch`. Mint deploy
        # keys.
        git_gate_plan = provision_git_gate_keys(plan)

        # Claim a TAP slot; the flock is held until teardown.
        slot, lock = netpool.from_settings(plan.settings.infra).allocate(plan.slug)
        stack.callback(lock.close)
        info(f"firecracker slot {slot.iface}: host={slot.host_ip} "
             f"guest={slot.guest_ip}")

        # Register by guest IP (unspoofable via the /31 TAP and nft) and
        # provision git-gate state.
        token_values = registration_tokens(plan)
        teardown_timeout = resolve_teardown_timeout(host_db_path())
        service = FirecrackerInfraService(plan.settings)
        ctx = launch_consolidated(
            plan.egress_plan, git_gate_plan,
            service=service,
            guest_ip=slot.guest_ip,
            tokens=token_values,
        )
        stack.callback(deprovision_consolidated, ctx.bottle_id, service=service,
                       timeout=teardown_timeout)

        # Stage the gateway CA and point git-gate and supervise at the slot's
        # host TAP IP, which DNATs to the gateway VM.
        egress_plan = stage_gateway_ca(plan, ctx.gateway_ca_pem)
        git_gate_url, supervise_url = gateway_agent_urls(
            plan, slot.host_ip, git_gate_plan)
        plan = dataclasses.replace(
            plan,
            git_gate_plan=git_gate_plan,
            egress_plan=egress_plan,
            identity_token=ctx.identity_token,
            env_var_secret=ctx.env_var_secret,
            # The identity token as proxy credentials; the gateway checks and
            # strips it.
            agent_proxy_url=(
                f"http://bottle:{ctx.identity_token}"
                f"@{slot.host_ip}:{plan.settings.infra.egress_port}"
            ),
            agent_git_gate_url=git_gate_url,
            agent_supervise_url=supervise_url,
        )

        # Build the per-bottle rootfs and key, then boot. Cleanup takes the
        # same lock, held until the VMM exists so a new run dir is never taken
        # for an orphan.
        with lifecycle_lock.hold():
            run_dir = util.cache_dir() / "run" / plan.slug
            run_dir.mkdir(parents=True, exist_ok=True)
            # Remove the run dir (~1G rootfs) on teardown; registered first so
            # it runs after `vm.terminate`.
            stack.callback(lambda: shutil.rmtree(run_dir, ignore_errors=True))
            rootfs = run_dir / "rootfs.ext4"
            util.make_bottle_rootfs(agent_base, rootfs)
            private_key, pubkey = util.generate_keypair(run_dir)

            vm = firecracker_vm.boot(
                name=plan.container_name,
                kernel=util.kernel_path(plan.settings.infra),
                rootfs=rootfs,
                tap=slot.iface,
                guest_ip=slot.guest_ip,
                host_ip=slot.host_ip,
                pubkey=pubkey,
                run_dir=run_dir,
                # Its own session, so it outlives a host controller restart;
                # the next controller adopts it.
                detached=True,
            )
        stack.callback(vm.terminate)
        firecracker_vm.wait_for_ssh(vm, private_key)
        persist_env_var_secret(private_key, slot.guest_ip, ctx.env_var_secret)

        # Prove the VM can't reach the host directly before the agent runs.
        isolation_probe.verify_isolation(private_key, slot.guest_ip)

        bottle = FirecrackerBottle(
            plan.container_name,
            private_key=private_key,
            guest_ip=slot.guest_ip,
            guest_env=_agent_guest_env(plan, slot.host_ip),
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            harness=plan.harness,
            terminal_title=terminal_title(plan.spec),
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
        )
        _smoke_test_published_base(
            bottle, agent_base, runtime_for(plan.harness).smoke_test)
        bottle.prompt_path = provision(plan, bottle)

        bottle.registry_id = ctx.bottle_id
        yield bottle
    finally:
        teardown()


def _smoke_test_published_base(
    bottle: FirecrackerBottle, agent_base: Path, smoke_test: tuple[str, ...],
) -> None:
    """Run the provider's smoke test in the first bottle booted from a
    published ext4; a `.smoked` marker skips it after that."""
    marker = agent_base.with_suffix(".smoked")
    if not smoke_test or not agent_base.is_file() or marker.is_file():
        return
    result = bottle.exec(shlex.join(smoke_test))
    if result.returncode != 0:
        die(f"published agent image {agent_base.name} failed its smoke test "
            f"({' '.join(smoke_test)}): {(result.stderr or result.stdout).strip()}")
    marker.write_text("ok\n")


def build_or_load_agent_base(plan: FirecrackerBottlePlan) -> Path:
    """The agent base (a rootfs dir or a prepared ext4) for
    `util.make_bottle_rootfs`, in order: a committed snapshot, the published
    ext4 or an overlay on the pinned OCI image, else the bottle's Dockerfile
    built in the infra VM. A custom `agent_provider.dockerfile` is always
    built."""
    committed = read_committed_image(plan.slug)
    committed_tar = committed_rootfs_path(plan.slug)
    if committed and committed_tar.is_file():
        info(f"resuming from committed rootfs {committed_tar}")
        return util.build_committed_rootfs_dir(committed_tar, plan.settings.infra)
    cached_base = (_cached_agent_base(plan)
                   if plan.spec.image_policy == "prefer-cached" else None)
    if cached_base is not None:
        info(f"using cached agent rootfs {cached_base.name}")
        return cached_base
    if plan.spec.image_policy == "cached":
        cached = _cached_agent_base(plan)
        if cached is None:
            die(
                f"cached agent rootfs for {plan.image!r} not found; "
                "run without --cached-images to build it"
            )
        info(f"using cached agent rootfs {cached.name}")
        return cached
    harness = plan.harness
    smoke = runtime_for(harness).smoke_test
    pin = _agent_pin(plan)
    if pin is None:
        return image_builder.build_agent_rootfs_dir(
            Path(plan.dockerfile_path), plan.settings, image_tag=plan.image, smoke_test=smoke)
    overlay = plan.manifest.agent.agent_provider.overlay
    if overlay.is_empty():
        return image_builder.acquire_agent_image_ext4(pin, plan.settings)
    return image_builder.acquire_agent_overlay_rootfs_dir(
        pin, overlay, plan.settings, harness=harness,
        source_root=Path(plan.spec.user_cwd), smoke_test=smoke,
    )


def _agent_pin(plan: FirecrackerBottlePlan) -> Artifact | None:
    """The published artifact this base comes from (the ext4, or the OCI image
    an overlay builds on). None means build the bottle's Dockerfile; an overlay
    without a published base fails closed."""
    provider = plan.manifest.agent.agent_provider
    if provider.builds_custom_image:
        return None
    overlay = provider.overlay
    try:
        pin = agent_image.resolve_agent_pin(
            plan.harness,
            fmt=FORMAT_EXT4 if overlay.is_empty() else FORMAT_OCI,
        )
    except ArtifactsManifestError as e:
        die(str(e))
    if pin is None and not overlay.is_empty():
        raise agent_overlay.no_published_base_error(plan.harness)
    return pin


def _cached_agent_base(plan: FirecrackerBottlePlan) -> Path | None:
    """The cached base `build_or_load_agent_base` would use, or None."""
    pin = _agent_pin(plan)
    if pin is None:
        return image_builder.cached_agent_rootfs_dir(
            Path(plan.dockerfile_path), plan.settings.infra)
    overlay = plan.manifest.agent.agent_provider.overlay
    if overlay.is_empty():
        return image_builder.cached_agent_image_ext4(pin, plan.settings.infra)
    return image_builder.cached_agent_overlay_rootfs_dir(
        pin, overlay, plan.settings.infra, harness=plan.harness,
        source_root=Path(plan.spec.user_cwd),
    )


def stale_checks(plan: FirecrackerBottlePlan) -> None:
    """Raise when the selected cached base is stale."""
    if plan.spec.image_policy not in ("cached", "prefer-cached"):
        return
    committed = read_committed_image(plan.slug)
    committed_tar = committed_rootfs_path(plan.slug)
    if committed and committed_tar.is_file():
        check_stale_path(f"agent rootfs {committed_tar}", committed_tar)
        return
    cached = _cached_agent_base(plan)
    if cached is not None:
        marker = cached.with_suffix(".ready") if cached.is_file() else cached / ".bb-ready"
        check_stale_path(f"agent rootfs {cached}", marker)


# --- agent guest env ---

def _agent_guest_env(plan: FirecrackerBottlePlan, host_ip: str) -> dict[str, str]:
    """Env for every agent and exec call over SSH, since the VM has no process
    env."""
    # Carries the identity token (set in `launch`).
    proxy_url = plan.agent_proxy_url or f"http://{host_ip}:{plan.settings.infra.egress_port}"
    no_proxy = f"localhost,127.0.0.1,{host_ip}"
    env: dict[str, str] = {
        "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url,
        "https_proxy": proxy_url, "http_proxy": proxy_url,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        # Name the global gitconfig explicitly so the insteadOf rules always
        # apply.
        "GIT_CONFIG_GLOBAL": f"{plan.guest_home}/.gitconfig",
        "NODE_EXTRA_CA_CERTS": AGENT_CA_PATH,
        "SSL_CERT_FILE": AGENT_CA_BUNDLE,
        "REQUESTS_CA_BUNDLE": AGENT_CA_BUNDLE,
    }
    if plan.agent_git_gate_url:
        env["GIT_GATE_URL"] = plan.agent_git_gate_url
    if plan.agent_supervise_url:
        env["MCP_SUPERVISE_URL"] = plan.agent_supervise_url
    if plan.env_var_secret:
        env[ENV_VAR_SECRET_NAME] = plan.env_var_secret
    for entry in Egress().agent_env_entries(plan.egress_plan):
        key, _, value = entry.partition("=")
        env[key] = value
    env.update(plan.agent_provision.guest_env)
    # Forwarded values, passed explicitly.
    env.update(plan.forwarded_env)
    return env
