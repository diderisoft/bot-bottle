"""The `GitGate` host service the backend drives at launch: build the plan,
provision and revoke deploy keys, and check upstream host keys.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from ..contract.git_trailers import TRAILER_KEY, TRAILER_VALUE_RE, trailer_mode
from ..lib.log import warn
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

_TRAILER_FILE = "trailer.json"


def save_trailer_settings(stage_dir: Path, trailer: str, mode: str) -> None:
    """Keep the trailer check's value and mode in the git-gate state dir, so
    a gateway re-provision applies the same check."""
    path = stage_dir / _TRAILER_FILE
    path.write_text(json.dumps({"trailer": trailer, "mode": mode}))
    path.chmod(0o600)


def load_trailer_settings(stage_dir: Path) -> tuple[str, str]:
    """(trailer, mode) as `save_trailer_settings` kept them; ("", "") when
    they can't be read, which git-gate refuses pushes over."""
    try:
        data = json.loads((stage_dir / _TRAILER_FILE).read_text(encoding="utf-8"))
        return str(data["trailer"]), trailer_mode(str(data["mode"]))
    except (OSError, ValueError, KeyError, TypeError):
        return "", ""


BASELINE_FILE = "baseline-commits"


def workspace_baseline(
    workspace: Path,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    """The commits `workspace`'s repo has that its remote-tracking refs don't
    reach (`rev-list --all --not --remotes`): the operator's unpushed work
    when the workspace is copied in. Commits a remote reaches are upstream,
    which git-gate already exempts. [] when git can't read the repo."""
    try:
        proc = run(["git", "-C", str(workspace), "rev-list", "--all", "--not", "--remotes"],
                   capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        warn(f"git-gate: couldn't list {workspace}'s unpushed commits; commits "
             f"already in the workspace will need the {TRAILER_KEY} trailer to push")
        return []
    return proc.stdout.split()


def save_baseline(stage_dir: Path, commits: Iterable[str]) -> Path:
    """Add `commits` to the bottle's baseline file (kept across resumes,
    which add the host repo's new unpushed commits) and return its path. The
    file is written even when empty."""
    path = stage_dir / BASELINE_FILE
    try:
        known = path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        known = []
    merged = list(dict.fromkeys([*known, *commits]))
    path.write_text("".join(f"{sha}\n" for sha in merged), encoding="utf-8")
    path.chmod(0o600)
    return path


def load_baseline(stage_dir: Path) -> Path:
    """The baseline file `save_baseline` wrote."""
    return stage_dir / BASELINE_FILE


class GitGate:
    """The stateless git-gate host service."""

    def prepare(self, bottle: ManifestBottle, slug: str, stage_dir: Path,
                *, trailer: str, baseline: Iterable[str] = ()) -> GitGatePlan:
        """Compute the upstream table from `bottle.git` and write the scripts
        under `stage_dir`. No side effects beyond the files. Forge upstreams
        get their identity file at launch. `trailer` is the `Bot-Bottle`
        value commits must carry, checked as `bottle.git_trailers` says;
        `baseline` is the commits the workspace brought in, which needn't
        carry it. Both are saved with the scripts, for a re-provision."""
        if not TRAILER_VALUE_RE.fullmatch(trailer):
            raise ValueError(f"git-gate: unsafe trailer value {trailer!r}")
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
                    writable=u.writable,
                )
            )
        mode = trailer_mode(bottle.git_trailers)
        save_trailer_settings(stage_dir, trailer, mode)
        return GitGatePlan(
            slug=slug,
            hook_script=hook,
            access_hook_script=access_hook,
            gitleaks_config=gitleaks_config,
            upstreams=tuple(upstreams_with_files),
            trailer=trailer,
            trailer_mode=mode,
            baseline_file=save_baseline(stage_dir, baseline),
        )

    def provision_dynamic_keys(
        self, bottle: ManifestBottle, plan: GitGatePlan, stage_dir: Path,
        secrets: SecretSource,
    ) -> GitGatePlan:
        """Mint the forge deploy keys (forge tokens from `secrets`) and return
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
