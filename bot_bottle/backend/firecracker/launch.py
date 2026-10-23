"""Launch flow for the Firecracker backend.

Per bottle:
  1. build the agent rootfs in a builder VM (buildah, no host docker), or
     resume a frozen bottle from its committed rootfs tar; cache the ext4;
  2. ensure the per-host orchestrator + shared gateway are up;
  3. claim a free TAP pool slot (rootless flock);
  4. register the bottle on the orchestrator by the VM's guest IP (the
     attribution key) and provision its git-gate state into the gateway;
  5. boot the microVM on that TAP; wait for SSH;
  6. provision (shared gateway CA, prompt, skills, workspace, git, supervise)
     over SSH.

The per-bottle Docker sidecar bundle is gone. The shared gateway (its own
data-plane VM) handles egress / git-gate / supervise for every VM; the nft
netpool's PREROUTING DNAT routes the VMs' gateway-port traffic to that gateway
VM, and the nft table's `ct status dnat accept` rule in the forward chain lets
it pass. The VM still sends to `host_tap_ip:PORT` — the address its world is,
by nft design, limited to.

Isolation is enforced by the operator-provisioned nft table (checked
fail-closed in preflight): a VM reaches only the gateway (DNAT'd from the host
TAP IP) and nothing else — not even the orchestrator control plane.
"""

from __future__ import annotations

import dataclasses
import shlex
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator

from ... import agent_image, agent_overlay
from ...agent_provider import runtime_for
from ...published_artifacts import FORMAT_EXT4, FORMAT_OCI, Artifact, ArtifactsManifestError
from ...bottle_state import committed_rootfs_path, read_committed_image
from ...egress import Egress
from ...image_cache import check_stale_path
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



@contextmanager
def launch(
    plan: FirecrackerBottlePlan,
    agent_base: Path,
    *,
    provision: Callable[[FirecrackerBottlePlan, "FirecrackerBottle"], str | None],
) -> Generator[FirecrackerBottle, None, None]:
    """Build, launch, and provision a Firecracker bottle via the consolidated
    orchestrator. Teardown on exit."""
    teardown = LaunchTeardown(plan, "firecracker")
    stack = teardown.stack

    try:
        # Step 1 (rootfs resolution/build) runs in BottleBackend.launch before
        # this context starts resources. ``agent_base`` is the selected cache,
        # fresh build, or committed snapshot.
        # Step 2: mint the git-gate dynamic (gitea) deploy keys, if any.
        git_gate_plan = provision_git_gate_keys(plan)

        # Step 3: claim a TAP slot; the flock is held until teardown.
        slot, lock = netpool.from_settings(plan.settings.infra).allocate(plan.slug)
        stack.callback(lock.close)
        info(f"firecracker slot {slot.iface}: host={slot.host_ip} "
             f"guest={slot.guest_ip}")

        # Step 4: register on the orchestrator + provision this bottle's
        # git-gate state into the shared gateway.
        # Attribution is by the VM's guest IP (unspoofable via /31 TAP + nft).
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

        # Step 5: stage the shared gateway CA for the provisioner to copy
        # over SSH, and point the agent's git-gate insteadOf rewrites and
        # supervise MCP URL at the shared gateway (reached at the slot's host
        # TAP IP — the VM sends there and the nft netpool DNAT routes to the
        # gateway VM).
        egress_plan = stage_gateway_ca(plan, ctx.gateway_ca_pem)
        git_gate_url, supervise_url = gateway_agent_urls(
            plan, slot.host_ip, git_gate_plan)
        plan = dataclasses.replace(
            plan,
            git_gate_plan=git_gate_plan,
            egress_plan=egress_plan,
            identity_token=ctx.identity_token,
            env_var_secret=ctx.env_var_secret,
            # Deliver the identity token as egress proxy credentials — clients
            # honor `HTTPS_PROXY=http://id:token@gw` without app changes; the
            # gateway reads Proxy-Authorization, validates the (source_ip,
            # token) pair, and strips it before upstream.
            agent_proxy_url=(
                f"http://bottle:{ctx.identity_token}"
                f"@{slot.host_ip}:{plan.settings.infra.egress_port}"
            ),
            agent_git_gate_url=git_gate_url,
            agent_supervise_url=supervise_url,
        )

        # Step 6: build the per-bottle rootfs + SSH key, then boot.
        # Cleanup takes the same lock while refreshing its process snapshot.
        # Hold it until the VMM exists so a newly-created run dir can never be
        # mistaken for an orphan in the build-before-boot window.
        with lifecycle_lock.hold():
            run_dir = util.cache_dir() / "run" / plan.slug
            run_dir.mkdir(parents=True, exist_ok=True)
            # Remove the run dir on teardown so the per-bottle rootfs.ext4 (~1G)
            # doesn't leak. Registered before vm.terminate below so it runs
            # *after* it (ExitStack is LIFO).
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
            )
        stack.callback(vm.terminate)
        firecracker_vm.wait_for_ssh(vm, private_key)
        persist_env_var_secret(private_key, slot.guest_ip, ctx.env_var_secret)

        # Authoritative fail-closed egress-boundary check, before the agent
        # runs: prove the VM cannot reach the host directly.
        isolation_probe.verify_isolation(private_key, slot.guest_ip)

        bottle = FirecrackerBottle(
            plan.container_name,
            private_key=private_key,
            guest_ip=slot.guest_ip,
            guest_env=_agent_guest_env(plan, slot.host_ip),
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            agent_provider_template=plan.agent_provider_template,
            terminal_title=terminal_title(plan.spec),
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
        )
        _smoke_test_published_base(
            bottle, agent_base, runtime_for(plan.agent_provider_template).smoke_test)
        bottle.prompt_path = provision(plan, bottle)

        yield bottle
    finally:
        teardown()


def _smoke_test_published_base(
    bottle: FirecrackerBottle, agent_base: Path, smoke_test: tuple[str, ...],
) -> None:
    """Run the provider's smoke test once in the first bottle booted from a
    prepared published ext4, as docker does after a pull. Images built here
    are smoke-tested before export; a published ext4 can only be run by
    booting it, so the first launch does it and a `.smoked` marker skips it
    after that."""
    marker = agent_base.with_suffix(".smoked")
    if not smoke_test or not agent_base.is_file() or marker.is_file():
        return
    result = bottle.exec(shlex.join(smoke_test))
    if result.returncode != 0:
        die(f"published agent image {agent_base.name} failed its smoke test "
            f"({' '.join(smoke_test)}): {(result.stderr or result.stdout).strip()}")
    marker.write_text("ok\n")


def build_or_load_agent_base(plan: FirecrackerBottlePlan) -> Path:
    """The agent's base: a rootfs dir or a prepared ext4 file, which
    `util.make_bottle_rootfs` turns into this bottle's disk. In order: a
    committed snapshot (freeze/migrate) resumed from the freezer's tar; the
    release-pinned published ext4, or an overlay built on the pinned OCI
    image; else the bottle's Dockerfile, built in the infra VM. A
    custom agent_provider.dockerfile is built as written, never replaced by
    the published default for its template."""
    committed = read_committed_image(plan.slug)
    committed_tar = committed_rootfs_path(plan.slug)
    if committed and committed_tar.is_file():
        info(f"resuming from committed rootfs {committed_tar}")
        return util.build_committed_rootfs_dir(committed_tar, plan.settings.infra)
    if plan.spec.image_policy == "cached":
        cached = _cached_agent_base(plan)
        if cached is None:
            die(
                f"cached agent rootfs for {plan.image!r} not found; "
                "run without --cached-images to build it"
            )
        info(f"using cached agent rootfs {cached.name}")
        return cached
    template = plan.agent_provider_template
    smoke = runtime_for(template).smoke_test
    pin = _agent_pin(plan)
    if pin is None:
        return image_builder.build_agent_rootfs_dir(
            Path(plan.dockerfile_path), plan.settings, image_tag=plan.image, smoke_test=smoke)
    overlay = plan.manifest.bottle.agent_provider.overlay
    if overlay.is_empty():
        return image_builder.acquire_agent_image_ext4(pin, plan.settings)
    return image_builder.acquire_agent_overlay_rootfs_dir(
        pin, overlay, plan.settings, template=template,
        source_root=Path(plan.spec.user_cwd), smoke_test=smoke,
    )


def _agent_pin(plan: FirecrackerBottlePlan) -> Artifact | None:
    """The published artifact this bottle's base comes from: the ext4 image,
    or the OCI image an overlay builds on. None means build the bottle's own
    Dockerfile; an overlay with no published base fails closed instead of
    building the default without it."""
    provider = plan.manifest.bottle.agent_provider
    if provider.builds_custom_image:
        return None
    overlay = provider.overlay
    try:
        pin = agent_image.resolve_agent_pin(
            plan.agent_provider_template,
            fmt=FORMAT_EXT4 if overlay.is_empty() else FORMAT_OCI,
        )
    except ArtifactsManifestError as e:
        die(str(e))
    if pin is None and not overlay.is_empty():
        raise agent_overlay.no_published_base_error(plan.agent_provider_template)
    return pin


def _cached_agent_base(plan: FirecrackerBottlePlan) -> Path | None:
    """The cached base `build_or_load_agent_base` would produce, or None."""
    pin = _agent_pin(plan)
    if pin is None:
        return image_builder.cached_agent_rootfs_dir(
            Path(plan.dockerfile_path), plan.settings.infra)
    overlay = plan.manifest.bottle.agent_provider.overlay
    if overlay.is_empty():
        return image_builder.cached_agent_image_ext4(pin, plan.settings.infra)
    return image_builder.cached_agent_overlay_rootfs_dir(
        pin, overlay, plan.settings.infra, template=plan.agent_provider_template,
        source_root=Path(plan.spec.user_cwd),
    )


def stale_checks(plan: FirecrackerBottlePlan) -> None:
    """Raise when the cached base selected by this plan is stale."""
    if plan.spec.image_policy != "cached":
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


# --- agent guest env -------------------------------------------------

def _agent_guest_env(plan: FirecrackerBottlePlan, host_ip: str) -> dict[str, str]:
    """Env injected into every agent/exec call over SSH. The VM has no
    baked process env (it just runs init), so the proxy/CA/git/supervise
    wiring is applied per-invocation."""
    # Carries the identity token as proxy credentials (set in `launch`).
    proxy_url = plan.agent_proxy_url or f"http://{host_ip}:{plan.settings.infra.egress_port}"
    no_proxy = f"localhost,127.0.0.1,{host_ip}"
    env: dict[str, str] = {
        "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url,
        "https_proxy": proxy_url, "http_proxy": proxy_url,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        # Rootfs export can leave Git's implicit XDG paths unreadable even
        # after the runtime repair. Bypass that discovery and name the
        # provisioned global config explicitly so insteadOf can never fall
        # through to the credential-bearing upstream URL.
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
    # Forwarded env carries resolved values; the VM can't inherit them from
    # a `docker run --env NAME`.
    env.update(plan.forwarded_env)
    return env
