"""Launch flow for the Firecracker backend (PRD 0070, consolidated).

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
import os
import shutil
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...agent_provider import runtime_for
from ...bottle_state import (
    committed_rootfs_path,
    egress_state_dir,
    git_gate_state_dir,
    read_committed_image,
)
from ...egress import Egress
from ...git_gate import GitGate
from ...image_cache import check_stale_path
from ...log import die, info, warn
from ...supervisor.types import SUPERVISE_PORT
from ..docker.egress import EGRESS_PORT
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from . import firecracker_vm, image_builder, isolation_probe, netpool, util
from .bottle import FirecrackerBottle
from .bottle_plan import FirecrackerBottlePlan
from ...orchestrator.store.config_store import resolve_teardown_timeout
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from .consolidated_launch import (
    launch_consolidated,
    persist_env_var_secret,
    deprovision_consolidated,
)


_GIT_HTTP_PORT = 9420


@contextmanager
def launch(
    plan: FirecrackerBottlePlan,
    agent_base: Path,
    *,
    provision: Callable[[FirecrackerBottlePlan, "FirecrackerBottle"], str | None],
) -> Generator[FirecrackerBottle, None, None]:
    """Build, launch, and provision a Firecracker bottle via the consolidated
    orchestrator. Teardown on exit."""
    stack = ExitStack()
    bottle_for_revoke = plan.manifest.bottle
    git_gate_dir_for_revoke = git_gate_state_dir(plan.slug)

    def teardown() -> None:
        teardown_exc: BaseException | None = None
        try:
            stack.close()
        except BaseException as exc:  # noqa: W0718 - teardown must continue
            teardown_exc = exc
            warn(f"firecracker teardown failed: {exc!r}")
        GitGate().revoke_provisioned_keys(bottle_for_revoke, git_gate_dir_for_revoke)
        if teardown_exc is not None:
            raise teardown_exc

    try:
        # Step 1 (rootfs resolution/build) runs in BottleBackend.launch before
        # this context starts resources. ``agent_base`` is the selected cache,
        # fresh build, or committed snapshot.
        # Step 2: mint the git-gate dynamic (gitea) deploy keys, if any.
        git_gate_plan = plan.git_gate_plan
        if git_gate_plan.upstreams:
            git_gate_plan = GitGate().provision_dynamic_keys(
                plan.manifest.bottle, git_gate_plan, git_gate_state_dir(plan.slug),
            )

        # Step 3: claim a TAP slot; the flock is held until teardown.
        slot, lock = netpool.allocate(plan.slug)
        stack.callback(lock.close)
        info(f"firecracker slot {slot.iface}: host={slot.host_ip} "
             f"guest={slot.guest_ip}")

        # Step 4: register on the orchestrator + provision this bottle's
        # git-gate state into the shared gateway. The per-bottle egress tokens
        # are resolved from the host env now and handed to the orchestrator
        # (in memory) for the gateway to inject — the agent never sees them.
        # Attribution is by the VM's guest IP (unspoofable via /31 TAP + nft).
        effective_env = {**os.environ, **plan.agent_provision.provisioned_env}
        token_values = Egress().resolve_token_values(
            plan.egress_plan.token_env_map, effective_env,
        )
        teardown_timeout = resolve_teardown_timeout()
        ctx = launch_consolidated(
            plan.egress_plan, git_gate_plan,
            guest_ip=slot.guest_ip,
            image_ref=plan.image,
            tokens=token_values,
        )
        stack.callback(
            deprovision_consolidated, ctx.bottle_id,
            orchestrator_url=ctx.orchestrator_url,
            timeout=teardown_timeout,
        )

        # Step 5: install the SHARED gateway CA (replaces the per-bottle CA).
        # Write it to a stable host path so the provisioner can copy it over SSH.
        ca_dir = egress_state_dir(plan.slug) / "gateway-ca"
        ca_dir.mkdir(parents=True, exist_ok=True)
        ca_file = ca_dir / "gateway-ca.pem"
        ca_file.write_text(ctx.gateway_ca_pem)
        egress_plan = dataclasses.replace(
            plan.egress_plan,
            mitmproxy_ca_host_path=ca_file,
            mitmproxy_ca_cert_only_host_path=ca_file,
        )
        # Point the agent's git-gate insteadOf rewrites and supervise MCP URL
        # at the shared gateway (reached at the slot's host TAP IP — the VM
        # sends there and the nft netpool DNAT routes to the gateway VM).
        git_gate_url = (
            f"http://{slot.host_ip}:{_GIT_HTTP_PORT}" if git_gate_plan.upstreams else ""
        )
        supervise_url = (
            f"http://{slot.host_ip}:{SUPERVISE_PORT}/"
            if plan.supervise_plan is not None else ""
        )
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
                f"@{slot.host_ip}:{EGRESS_PORT}"
            ),
            agent_git_gate_url=git_gate_url,
            agent_supervise_url=supervise_url,
        )

        # Step 6: build the per-bottle rootfs + SSH key, then boot.
        run_dir = util.cache_dir() / "run" / plan.slug
        run_dir.mkdir(parents=True, exist_ok=True)
        # Remove the run dir on teardown so the per-bottle rootfs.ext4 (~1G)
        # doesn't leak. Registered before vm.terminate below so it runs *after*
        # it (ExitStack is LIFO): the VM is gone before we rm its rootfs.
        stack.callback(lambda: shutil.rmtree(run_dir, ignore_errors=True))
        rootfs = run_dir / "rootfs.ext4"
        util.build_rootfs_ext4(agent_base, rootfs)
        private_key, pubkey = util.generate_keypair(run_dir)

        vm = firecracker_vm.boot(
            name=plan.container_name,
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
            terminal_title=(
                f"{plan.spec.label} ({plan.spec.agent_name})"
                if plan.spec.label else plan.spec.agent_name
            ),
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
        )
        bottle.prompt_path = provision(plan, bottle)

        yield bottle
    finally:
        teardown()


def build_or_load_agent_base(plan: FirecrackerBottlePlan) -> Path:
    """Produce the agent's base rootfs dir. Primary path: build the Dockerfile
    inside a Firecracker builder VM (buildah, no host docker), smoke-testing
    the image before export. A committed snapshot (freeze/migrate) is resumed
    directly from the rootfs tar the freezer wrote — no host docker either."""
    committed = read_committed_image(plan.slug)
    committed_tar = committed_rootfs_path(plan.slug)
    if committed and committed_tar.is_file():
        info(f"resuming from committed rootfs {committed_tar}")
        return util.build_committed_rootfs_dir(committed_tar)
    dockerfile = Path(plan.dockerfile_path)
    if plan.spec.image_policy == "cached":
        cached = image_builder.cached_agent_rootfs_dir(dockerfile)
        if cached is None:
            die(
                f"cached agent rootfs for {plan.image!r} not found; "
                "run without --cached-images to build it"
            )
        info(f"using cached agent rootfs {cached.name}")
        return cached
    return image_builder.build_agent_rootfs_dir(
        dockerfile,
        image_tag=plan.image,
        smoke_test=runtime_for(plan.agent_provider_template).smoke_test,
    )


def stale_checks(plan: FirecrackerBottlePlan) -> None:
    """Raise when the cached rootfs selected by this plan is stale."""
    if plan.spec.image_policy != "cached":
        return
    committed = read_committed_image(plan.slug)
    committed_tar = committed_rootfs_path(plan.slug)
    if committed and committed_tar.is_file():
        check_stale_path(f"agent rootfs {committed_tar}", committed_tar)
        return
    cached = image_builder.cached_agent_rootfs_dir(Path(plan.dockerfile_path))
    if cached is not None:
        check_stale_path(f"agent rootfs {cached}", cached / ".bb-ready")


# --- agent guest env -------------------------------------------------

def _agent_guest_env(plan: FirecrackerBottlePlan, host_ip: str) -> dict[str, str]:
    """Env injected into every agent/exec call over SSH. The VM has no
    baked process env (it just runs init), so the proxy/CA/git/supervise
    wiring is applied per-invocation."""
    # Carries the identity token as proxy credentials (set in `launch`).
    proxy_url = plan.agent_proxy_url or f"http://{host_ip}:{EGRESS_PORT}"
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
    # Forwarded (bare-name) env: resolve host values now, since the VM
    # can't inherit them from a `docker run --env NAME`.
    for name in plan.forwarded_env:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env
