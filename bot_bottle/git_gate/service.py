"""The `GitGate` host service the backend drives at launch: build the plan,
provision and revoke deploy keys, and check upstream host keys.
"""

from __future__ import annotations

from pathlib import Path

from ..manifest import Manifest, ManifestBottle
from ..secret_source import SecretSource
from ..control.git_gate_render import (
    GitGateUpstream,
    git_gate_known_hosts_line,
    git_gate_render_access_hook,
    git_gate_render_gitleaks_config,
    git_gate_render_hook,
    git_gate_upstreams_for_bottle,
)
from .plan import GitGatePlan
from . import provision as _provision
from . import host_key as _host_key


def stage_git_gate_scripts(stage_dir: Path) -> tuple[Path, Path, Path]:
    """Render the pre-receive hook, access hook and gitleaks config from the
    current code into `stage_dir`; returns their paths in that order. Used at
    launch and on re-provision."""
    hook = stage_dir / "git_gate_pre_receive.sh"
    hook.write_text(git_gate_render_hook())
    hook.chmod(0o600)
    access_hook = stage_dir / "git_gate_access_hook.sh"
    access_hook.write_text(git_gate_render_access_hook())
    # Executable: the hook is exec'd directly. Provisioning re-applies +x in
    # the gateway (see `backend/provision_gateway.py`).
    access_hook.chmod(0o700)
    gitleaks_config = stage_dir / "git_gate_gitleaks.toml"
    gitleaks_config.write_text(git_gate_render_gitleaks_config())
    gitleaks_config.chmod(0o600)
    return hook, access_hook, gitleaks_config

class GitGate:
    """The stateless git-gate host service."""

    def prepare(self, bottle: ManifestBottle, slug: str, stage_dir: Path) -> GitGatePlan:
        """Compute the upstream table from `bottle.git` and write the scripts
        under `stage_dir`. No side effects beyond the files. gitea upstreams
        get their identity file at launch."""
        upstreams = git_gate_upstreams_for_bottle(bottle)
        hook, access_hook, gitleaks_config = stage_git_gate_scripts(stage_dir)
        upstreams_with_files: list[GitGateUpstream] = []
        for u in upstreams:
            known_hosts_file = Path()
            if u.known_host_key:
                known_hosts_file = stage_dir / f"{u.name}-known_hosts"
                known_hosts_file.write_text(
                    git_gate_known_hosts_line(
                        u.upstream_host, u.upstream_port, u.known_host_key,
                    )
                )
                known_hosts_file.chmod(0o600)
            upstreams_with_files.append(
                GitGateUpstream(
                    name=u.name,
                    upstream_url=u.upstream_url,
                    upstream_host=u.upstream_host,
                    upstream_port=u.upstream_port,
                    identity_file=u.identity_file,
                    known_host_key=u.known_host_key,
                    known_hosts_file=known_hosts_file,
                )
            )
        return GitGatePlan(
            slug=slug,
            hook_script=hook,
            access_hook_script=access_hook,
            gitleaks_config=gitleaks_config,
            upstreams=tuple(upstreams_with_files),
        )

    def provision_dynamic_keys(
        self, bottle: ManifestBottle, plan: GitGatePlan, stage_dir: Path,
        secrets: SecretSource,
    ) -> GitGatePlan:
        """Mint the gitea deploy keys (forge tokens from `secrets`) and return
        the plan with their identity files."""
        return _provision.provision_git_gate_dynamic_keys(bottle, plan, stage_dir, secrets)

    def revoke_provisioned_keys(
        self, bottle: ManifestBottle, stage_dir: Path, secrets: SecretSource,
    ) -> None:
        """Revoke every deploy key provisioned for `bottle`."""
        _provision.revoke_git_gate_provisioned_keys(bottle, stage_dir, secrets)

    def preflight_host_keys(
        self, manifest: Manifest, *, headless: bool, home_md: Path | None,
    ) -> Manifest:
        """Ensure every upstream has a host key (fetching, or failing
        headless). Returns the possibly updated manifest."""
        return _host_key.preflight_host_keys(
            manifest, headless=headless, home_md=home_md,
        )
