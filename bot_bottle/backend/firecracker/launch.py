"""Launch flow for the Firecracker backend.

Per bottle:
  1. mint the egress CA, build the agent image (docker), export it to a
     cached ext4 rootfs;
  2. claim a free TAP pool slot (rootless flock);
  3. bring up the Docker sidecar bundle, publishing egress / git-gate /
     supervise on the slot's host-side TAP IP at fixed ports;
  4. boot the microVM on that TAP; wait for SSH;
  5. provision (CA, prompt, skills, workspace, git, supervise) over SSH.

Isolation is enforced by the operator-provisioned nft table (checked
fail-closed in preflight): a VM reaches only its sidecar (DNAT'd from
the host TAP IP) and nothing else. The agent's HTTPS_PROXY therefore
points at `http://<host_tap_ip>:9099`, its only route to the world.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...bottle_state import (
    egress_state_dir,
    git_gate_state_dir,
    read_committed_image,
)
from ...egress import (
    EGRESS_ROUTES_IN_CONTAINER,
    egress_agent_env_entries,
    egress_resolve_token_values,
    egress_sidecar_env_entries,
)
from ...git_gate import (
    provision_git_gate_dynamic_keys,
    revoke_git_gate_provisioned_keys,
)
from ...log import die, info, warn
from ...supervise import DB_PATH_IN_CONTAINER, SUPERVISE_PORT
from ...util import expand_tilde
from ..docker.egress import (
    EGRESS_CA_IN_CONTAINER,
    EGRESS_PORT,
    egress_tls_init,
)
from ..docker.git_gate import (
    GIT_GATE_ACCESS_HOOK_IN_CONTAINER,
    GIT_GATE_CREDS_DIR_IN_CONTAINER,
    GIT_GATE_ENTRYPOINT_IN_CONTAINER,
    GIT_GATE_HOOK_IN_CONTAINER,
)
from ..docker.sidecar_bundle import (
    SIDECAR_BUNDLE_DOCKERFILE,
    SIDECAR_BUNDLE_IMAGE,
)
from ..util import AGENT_CA_BUNDLE, AGENT_CA_PATH
from . import firecracker_vm, isolation_probe, netpool, util
from .bottle import FirecrackerBottle
from .bottle_plan import FirecrackerBottlePlan


_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)
_GIT_HTTP_PORT = 9420
_GIT_GATE_READY_FILE = "/run/git-gate/ready"


def sidecar_container_name(slug: str) -> str:
    return f"bot-bottle-sidecars-{slug}"


@contextmanager
def launch(
    plan: FirecrackerBottlePlan,
    *,
    provision: Callable[[FirecrackerBottlePlan, "FirecrackerBottle"], str | None],
) -> Generator[FirecrackerBottle, None, None]:
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
        revoke_git_gate_provisioned_keys(bottle_for_revoke, git_gate_dir_for_revoke)
        if teardown_exc is not None:
            raise teardown_exc

    try:
        plan = _mint_certs(plan)
        plan = _build_agent_image(plan)

        # Claim a TAP slot; the flock is held until teardown closes it.
        slot, lock = netpool.allocate(plan.slug)
        stack.callback(lock.close)
        info(f"firecracker slot {slot.iface}: host={slot.host_ip} "
             f"guest={slot.guest_ip}")

        plan = _provision_git_gate_keys(plan)

        sidecar_name = sidecar_container_name(plan.slug)
        _force_remove_container(sidecar_name)
        _start_sidecar_bundle(plan, sidecar_name, slot.host_ip)
        stack.callback(_force_remove_container, sidecar_name)
        _stage_git_gate(plan, sidecar_name)

        plan = _stamp_agent_urls(plan, slot.host_ip)

        # Build the per-bottle rootfs + SSH key, then boot.
        base_dir = util.build_base_rootfs_dir(plan.image)
        run_dir = util.cache_dir() / "run" / plan.slug
        run_dir.mkdir(parents=True, exist_ok=True)
        rootfs = run_dir / "rootfs.ext4"
        util.build_rootfs_ext4(base_dir, rootfs)
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

        # Authoritative fail-closed egress-boundary check, before the
        # agent runs: prove the VM cannot reach the host directly.
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


def _mint_certs(plan: FirecrackerBottlePlan) -> FirecrackerBottlePlan:
    egress_ca_host, egress_ca_cert_only = egress_tls_init(egress_state_dir(plan.slug))
    egress_plan = dataclasses.replace(
        plan.egress_plan,
        mitmproxy_ca_host_path=egress_ca_host,
        mitmproxy_ca_cert_only_host_path=egress_ca_cert_only,
    )
    return dataclasses.replace(plan, egress_plan=egress_plan)


def _build_agent_image(plan: FirecrackerBottlePlan) -> FirecrackerBottlePlan:
    _docker_build(SIDECAR_BUNDLE_IMAGE, _REPO_DIR, dockerfile=SIDECAR_BUNDLE_DOCKERFILE)
    committed = read_committed_image(plan.slug)
    if committed and _image_exists(committed):
        info(f"using committed image {committed!r}")
        return dataclasses.replace(
            plan,
            agent_provision=dataclasses.replace(plan.agent_provision, image=committed),
        )
    _docker_build(plan.image, _REPO_DIR, dockerfile=plan.dockerfile_path)
    return plan


def _provision_git_gate_keys(plan: FirecrackerBottlePlan) -> FirecrackerBottlePlan:
    if not plan.git_gate_plan.upstreams:
        return plan
    git_gate_plan = provision_git_gate_dynamic_keys(
        plan.manifest.bottle, plan.git_gate_plan, git_gate_state_dir(plan.slug),
    )
    return dataclasses.replace(plan, git_gate_plan=git_gate_plan)


def _stamp_agent_urls(
    plan: FirecrackerBottlePlan, host_ip: str,
) -> FirecrackerBottlePlan:
    proxy_url = f"http://{host_ip}:{EGRESS_PORT}"
    supervise_url = (
        f"http://{host_ip}:{SUPERVISE_PORT}/" if plan.supervise_plan is not None else ""
    )
    git_gate_url = (
        f"http://{host_ip}:{_GIT_HTTP_PORT}" if plan.git_gate_plan.upstreams else ""
    )
    return dataclasses.replace(
        plan,
        agent_proxy_url=proxy_url,
        agent_git_gate_url=git_gate_url,
        agent_supervise_url=supervise_url,
    )


# --- sidecar bundle (Docker) ----------------------------------------

def _start_sidecar_bundle(
    plan: FirecrackerBottlePlan, sidecar_name: str, host_ip: str,
) -> None:
    argv = ["docker", "run", "--name", sidecar_name, "--detach", "--rm",
            "-e", f"BOT_BOTTLE_SIDECAR_DAEMONS={','.join(_sidecar_daemons(plan))}"]
    for entry in _sidecar_env_entries(plan):
        argv += ["-e", entry]
    for host_path, container_path, read_only in _sidecar_mounts(plan):
        argv += ["-v", f"{host_path}:{container_path}{':ro' if read_only else ''}"]
    # Publish on the slot's host TAP IP at fixed ports — each bottle
    # has a distinct host_ip, so fixed ports never collide, and the VM
    # reaches them at a stable, well-known address (its only route out).
    for port in _sidecar_ports(plan):
        argv += ["-p", f"{host_ip}:{port}:{port}"]
    argv.append(SIDECAR_BUNDLE_IMAGE)

    effective_env = {**dict(os.environ), **plan.agent_provision.provisioned_env}
    token_values = egress_resolve_token_values(
        plan.egress_plan.token_env_map, effective_env,
    )
    env = {**os.environ, **token_values}
    info(f"docker run sidecar bundle {sidecar_name} (published on {host_ip})")
    result = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        die(f"docker run for sidecar bundle {sidecar_name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}")


def _sidecar_daemons(plan: FirecrackerBottlePlan) -> tuple[str, ...]:
    daemons = ["egress"]
    if plan.git_gate_plan.upstreams:
        daemons += ["git-gate", "git-http"]
    if plan.supervise_plan is not None:
        daemons.append("supervise")
    return tuple(daemons)


def _sidecar_ports(plan: FirecrackerBottlePlan) -> tuple[int, ...]:
    ports = [EGRESS_PORT]
    if plan.git_gate_plan.upstreams:
        ports.append(_GIT_HTTP_PORT)
    if plan.supervise_plan is not None:
        ports.append(SUPERVISE_PORT)
    return tuple(ports)


def _sidecar_env_entries(plan: FirecrackerBottlePlan) -> tuple[str, ...]:
    env: list[str] = list(egress_sidecar_env_entries(plan.egress_plan))
    if plan.git_gate_plan.upstreams:
        env.append(f"BOT_BOTTLE_GIT_GATE_READY_FILE={_GIT_GATE_READY_FILE}")
    if plan.supervise_plan is not None:
        env += [
            f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            f"SUPERVISE_DB_PATH={DB_PATH_IN_CONTAINER}",
            f"SUPERVISE_PORT={SUPERVISE_PORT}",
        ]
    return tuple(env)


def _sidecar_mounts(
    plan: FirecrackerBottlePlan,
) -> tuple[tuple[str, str, bool], ...]:
    mounts: list[tuple[str, str, bool]] = []
    ep = plan.egress_plan
    mounts.append((str(ep.mitmproxy_ca_host_path.parent),
                   str(Path(EGRESS_CA_IN_CONTAINER).parent), False))
    if ep.routes:
        mounts.append((str(ep.routes_path.parent),
                       str(Path(EGRESS_ROUTES_IN_CONTAINER).parent), True))
    sp = plan.supervise_plan
    if sp is not None:
        mounts.append((str(sp.db_path.parent),
                       str(Path(DB_PATH_IN_CONTAINER).parent), False))
    return tuple(mounts)


def _stage_git_gate(plan: FirecrackerBottlePlan, sidecar_name: str) -> None:
    gp = plan.git_gate_plan
    if not gp.upstreams:
        return
    _docker_exec(sidecar_name, [
        "mkdir", "-p",
        str(Path(GIT_GATE_HOOK_IN_CONTAINER).parent),
        GIT_GATE_CREDS_DIR_IN_CONTAINER, "/git",
        str(Path(_GIT_GATE_READY_FILE).parent),
    ])
    for host_path, container_path in _git_gate_files(plan):
        _docker_cp(host_path, f"{sidecar_name}:{container_path}")
    _docker_exec(sidecar_name, [
        "sh", "-c",
        f"chmod 755 {GIT_GATE_ENTRYPOINT_IN_CONTAINER} "
        f"{GIT_GATE_HOOK_IN_CONTAINER} {GIT_GATE_ACCESS_HOOK_IN_CONTAINER} && "
        f"chmod 600 {GIT_GATE_CREDS_DIR_IN_CONTAINER}/* && "
        f"touch {_GIT_GATE_READY_FILE}",
    ])


def _git_gate_files(plan: FirecrackerBottlePlan) -> tuple[tuple[str, str], ...]:
    gp = plan.git_gate_plan
    files: list[tuple[str, str]] = [
        (str(gp.entrypoint_script), GIT_GATE_ENTRYPOINT_IN_CONTAINER),
        (str(gp.hook_script), GIT_GATE_HOOK_IN_CONTAINER),
        (str(gp.access_hook_script), GIT_GATE_ACCESS_HOOK_IN_CONTAINER),
    ]
    for upstream in gp.upstreams:
        files.append((expand_tilde(upstream.identity_file),
                      f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{upstream.name}-key"))
        if upstream.known_hosts_file:
            files.append((str(upstream.known_hosts_file),
                          f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{upstream.name}-known_hosts"))
    return tuple(files)


# --- agent guest env -------------------------------------------------

def _agent_guest_env(plan: FirecrackerBottlePlan, host_ip: str) -> dict[str, str]:
    """Env injected into every agent/exec call over SSH. The VM has no
    baked process env (it just runs init), so the proxy/CA/git/supervise
    wiring is applied per-invocation."""
    proxy_url = f"http://{host_ip}:{EGRESS_PORT}"
    no_proxy = f"localhost,127.0.0.1,{host_ip}"
    env: dict[str, str] = {
        "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url,
        "https_proxy": proxy_url, "http_proxy": proxy_url,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        "NODE_EXTRA_CA_CERTS": AGENT_CA_PATH,
        "SSL_CERT_FILE": AGENT_CA_BUNDLE,
        "REQUESTS_CA_BUNDLE": AGENT_CA_BUNDLE,
    }
    if plan.agent_git_gate_url:
        env["GIT_GATE_URL"] = plan.agent_git_gate_url
    if plan.agent_supervise_url:
        env["MCP_SUPERVISE_URL"] = plan.agent_supervise_url
    for entry in egress_agent_env_entries(plan.egress_plan):
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


# --- docker helpers --------------------------------------------------

def _docker_build(ref: str, context: str, *, dockerfile: str = "") -> None:
    info(f"docker build {ref}")
    args = ["docker", "build", "-t", ref]
    if dockerfile:
        if not os.path.isabs(dockerfile):
            dockerfile = os.path.join(context, dockerfile)
        args += ["-f", dockerfile]
    args.append(context)
    result = subprocess.run(args, check=False)
    if result.returncode != 0:
        die(f"docker build for {ref!r} failed")


def _image_exists(ref: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", ref],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def _force_remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def _docker_exec(name: str, argv: list[str]) -> None:
    result = subprocess.run(
        ["docker", "exec", name, *argv], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"docker exec in {name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}")


def _docker_cp(host_path: str, dest: str) -> None:
    result = subprocess.run(
        ["docker", "cp", host_path, dest], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"docker cp {host_path} -> {dest} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}")
