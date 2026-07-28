"""Bring-up reconcile: re-attach all running agent VMs to a freshly-booted gateway.

Called from the cold-boot branch of `FirecrackerInfraService.ensure_running()`
after `gateway.connect_to_orchestrator()` completes. Restores the three
gateway-dependent services for every live bottle:

  - **CA** — push the current (freshly-minted) gateway CA to each agent's
    trust store and run `update-ca-certificates`, so the agent trusts the
    new CA on its next egress call.
  - **git-gate** — re-provision each bottle's bare repos and per-repo creds
    in the gateway, rebuilt from the persisted host-side state dir.
  - **egress tokens** — read the agent's `ENV_VAR_SECRET` and feed
    `reprovision_bottles` to restore the orchestrator's in-memory tokens.

Per-bottle failures are caught, logged, and skipped so one unreachable VM
does not block the others or the gateway coming up (PRD 0081).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ...bottle_state import git_gate_state_dir
from ...gateway import GatewayError
from ...gateway.git_gate.render import GitGateUpstream
from ...git_gate.plan import GitGatePlan
from ...log import info
from ...orchestrator.client import OrchestratorClient, OrchestratorClientError
from ...orchestrator.reprovision import reprovision_bottles
from ..provision_gateway import provision_git_gate
from ..util import AGENT_CA_PATH
from . import cleanup, util
from .gateway import FirecrackerGateway

# Where persist_env_var_secret writes the key on the agent VM (matches
# consolidated_launch._ENV_VAR_SECRET_PATH — duplicated to avoid a
# circular import through infra.py).
_ENV_VAR_SECRET_PATH = "/run/bot-bottle/env-var-secret"


def _guest_ip_from_config(config_path: Path) -> str:
    """Read the agent VM's guest IP from its Firecracker config file."""
    try:
        config = json.loads(config_path.read_text())
        args = config["boot-source"]["boot_args"]
        ip_arg = next(p for p in args.split() if p.startswith("ip="))
        return ip_arg.removeprefix("ip=").split(":", 1)[0]
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return ""


def attach_bottled_agents_to_gateway(
    orchestrator_url: str, gateway: FirecrackerGateway,
) -> None:
    """Reconcile all live agent VMs against the freshly-booted gateway.

    Fetches the new CA, lists registered bottles, then for each live run dir
    pushes the CA, re-provisions git-gate, and restores egress tokens. Each
    per-bottle step is wrapped so a failure is logged and skipped."""
    try:
        ca_pem = gateway.ca_cert_pem()
    except GatewayError as exc:
        info(f"bring-up reconcile: could not fetch gateway CA, skipping: {exc}")
        return

    client = OrchestratorClient(orchestrator_url)
    try:
        bottles = client.list_bottles()
    except OrchestratorClientError as exc:
        info(f"bring-up reconcile: could not list bottles, skipping: {exc}")
        return

    source_ip_to_bottle_id: dict[str, str] = {}
    for b in bottles:
        source_ip = b.get("source_ip")
        bottle_id = b.get("bottle_id")
        if isinstance(source_ip, str) and isinstance(bottle_id, str):
            source_ip_to_bottle_id[source_ip] = bottle_id

    transport = gateway.provisioning_transport()
    secrets_by_ip: dict[str, str] = {}

    for run_dir in cleanup.live_run_dirs():
        slug = run_dir.name
        guest_ip = _guest_ip_from_config(run_dir / "config.json")
        private_key = run_dir / "bottle_id_ed25519"
        if not guest_ip or not private_key.is_file():
            continue

        # CA: push the gateway's current certificate to the agent's trust store.
        try:
            _push_ca(private_key, guest_ip, ca_pem)
        except Exception as exc:
            info(f"bring-up reconcile: CA push to {slug!r} failed: {exc}")

        # git-gate: re-provision repos + creds from the persisted state dir.
        bottle_id = source_ip_to_bottle_id.get(guest_ip)
        if bottle_id:
            try:
                _reprovision_git_gate(transport, bottle_id, slug)
            except Exception as exc:
                info(f"bring-up reconcile: git-gate for {slug!r} failed: {exc}")

        # egress tokens: read ENV_VAR_SECRET from the agent's tmpfs.
        proc = subprocess.run(
            util.ssh_base_argv(private_key, guest_ip) + [f"cat {_ENV_VAR_SECRET_PATH}"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            secrets_by_ip[guest_ip] = proc.stdout.strip()

    # Restore in-memory egress tokens for all bottles that exposed a key.
    if secrets_by_ip:
        try:
            count = reprovision_bottles(client, secrets_by_ip)
            if count:
                info(
                    f"bring-up reconcile: restored egress tokens for {count} bottle(s)"
                )
        except OrchestratorClientError as exc:
            info(f"bring-up reconcile: egress token restore failed: {exc}")


def _push_ca(private_key: Path, guest_ip: str, ca_pem: str) -> None:
    """SSH the gateway CA PEM into the agent VM and update its trust store.

    Runs as the SSH root user (the agent VM's dropbear accepts root). Each
    step uses check=True so a failure raises and the caller's except clause
    logs and continues."""
    ssh = util.ssh_base_argv(private_key, guest_ip)
    # mkdir is idempotent; rootfs builds may not preserve the target dir.
    subprocess.run(
        ssh + ["mkdir -p /usr/local/share/ca-certificates"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ssh + [f"cat > {AGENT_CA_PATH}"],
        input=ca_pem, text=True, capture_output=True, check=True,
    )
    subprocess.run(
        ssh + [f"chmod 644 {AGENT_CA_PATH} && update-ca-certificates"],
        capture_output=True, check=True,
    )


def _reprovision_git_gate(
    transport: object, bottle_id: str, slug: str,
) -> None:
    """Re-provision one bottle's git-gate repos and creds from its state dir.

    Reads `upstreams.json` (written by `provision_git_gate_dynamic_keys` at
    launch) to reconstruct the GitGateUpstream table, then calls
    `provision_git_gate` which places the credential files and inits the bare
    repos in the gateway. No-op when there is no `upstreams.json` (the bottle
    has no git upstreams)."""
    state_dir = git_gate_state_dir(slug)
    upstreams_file = state_dir / "upstreams.json"
    if not upstreams_file.exists():
        return

    raw = json.loads(upstreams_file.read_text())
    upstreams: list[GitGateUpstream] = []
    for u in raw:
        name = u["name"]
        # The key in the state dir is preferred: gitea deploy keys are written
        # there by provision_git_gate_dynamic_keys; static keys keep their
        # original manifest path.
        key_in_state = state_dir / f"{name}-key"
        identity_file = (
            str(key_in_state) if key_in_state.is_file()
            else u.get("identity_file", "")
        )
        known_hosts = state_dir / f"{name}-known_hosts"
        upstreams.append(GitGateUpstream(
            name=name,
            upstream_url=u["upstream_url"],
            upstream_host=u.get("upstream_host", ""),
            upstream_port=u.get("upstream_port", ""),
            identity_file=identity_file,
            known_host_key=u.get("known_host_key", ""),
            known_hosts_file=known_hosts if known_hosts.is_file() else Path(),
        ))

    if not upstreams:
        return

    plan = GitGatePlan(
        slug=slug,
        entrypoint_script=state_dir / "git_gate_entrypoint.sh",
        hook_script=state_dir / "git_gate_pre_receive.sh",
        access_hook_script=state_dir / "git_gate_access_hook.sh",
        upstreams=tuple(upstreams),
    )
    provision_git_gate(transport, bottle_id, plan)  # type: ignore[arg-type]


__all__ = ["attach_bottled_agents_to_gateway"]
