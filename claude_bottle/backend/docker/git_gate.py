"""DockerGitGate — the Docker-specific lifecycle for the per-agent
git-gate sidecar (PRD 0008). Inherits the platform-agnostic prepare
step (upstream lift + entrypoint/hook render) from `GitGate`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...git_gate import GitGate, GitGatePlan, git_gate_known_hosts_line
from ...log import die, info, warn
from ...util import expand_tilde
from . import util as docker_mod


GIT_GATE_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_GIT_GATE_IMAGE",
    "claude-bottle-git-gate:latest",
)

GIT_GATE_DOCKERFILE = "Dockerfile.git-gate"

GIT_GATE_ENTRYPOINT_IN_CONTAINER = "/git-gate-entrypoint.sh"
GIT_GATE_HOOK_IN_CONTAINER = "/etc/git-gate/pre-receive"
GIT_GATE_CREDS_DIR_IN_CONTAINER = "/git-gate/creds"

# git daemon's default listening port. Surfaced as a constant because
# integration tests probe the gate on it.
GIT_GATE_PORT = 9418

# Repo root, for `docker build` context. Resolved from this file's
# location: claude_bottle/backend/docker/git_gate.py → repo root.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def git_gate_container_name(slug: str) -> str:
    return f"claude-bottle-git-gate-{slug}"


def git_gate_host(slug: str) -> str:
    """The hostname the agent's git client should connect to (same as
    the container name — Docker's embedded DNS resolves it on the
    `--internal` network)."""
    return git_gate_container_name(slug)


def build_git_gate_image() -> None:
    """Build the git-gate image from `Dockerfile.git-gate`. Called by
    `DockerGitGate.start`; exposed at module level so integration
    tests can build it without running the full launch pipeline."""
    docker_mod.build_image(GIT_GATE_IMAGE, _REPO_DIR, dockerfile=GIT_GATE_DOCKERFILE)


class DockerGitGate(GitGate):
    """Brings the git-gate sidecar up and down via Docker."""

    def start(self, plan: GitGatePlan) -> str:
        """Boot the gate sidecar:
          1. Build the gate image (no-op when cache is hot).
          2. `docker create` on the internal network with the canonical
             name; the image's ENTRYPOINT runs the cp'd entrypoint
             script at start time.
          3. `docker cp` the entrypoint, the shared pre-receive hook,
             and each upstream's identity + known_hosts into the
             container.
          4. Attach to the per-agent egress network so the gate can
             reach the real upstream.
          5. `docker start`.
        Returns the container name (the target passed to `.stop`)."""
        if not plan.upstreams:
            die("DockerGitGate.start called with no upstreams; caller should skip")
        if not plan.internal_network or not plan.egress_network:
            die(
                "DockerGitGate.start: internal_network / egress_network must be "
                "populated on the plan before start"
            )
        if not plan.entrypoint_script.is_file():
            die(
                f"git-gate entrypoint missing at {plan.entrypoint_script}; "
                f"GitGate.prepare must run first"
            )
        if not plan.hook_script.is_file():
            die(
                f"git-gate hook missing at {plan.hook_script}; "
                f"GitGate.prepare must run first"
            )

        build_git_gate_image()

        name = git_gate_container_name(plan.slug)
        info(f"starting git-gate sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            GIT_GATE_IMAGE,
        ]
        if subprocess.run(
            create_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0:
            die(f"failed to create git-gate sidecar {name}")

        # Order matters: entrypoint + hook first so they're present
        # when docker start fires. Per-upstream creds afterwards.
        stage_dir = plan.entrypoint_script.parent
        cps: list[tuple[str, str, str]] = [
            (str(plan.entrypoint_script), GIT_GATE_ENTRYPOINT_IN_CONTAINER, "entrypoint"),
            (str(plan.hook_script), GIT_GATE_HOOK_IN_CONTAINER, "pre-receive hook"),
        ]
        for u in plan.upstreams:
            keypath = expand_tilde(u.identity_file)
            cps.append((
                keypath,
                f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{u.name}-key",
                f"upstream key for '{u.name}'",
            ))
            if u.known_host_key:
                hosts_path = stage_dir / f"git_gate_known_hosts_{u.name}"
                hosts_path.write_text(
                    git_gate_known_hosts_line(
                        u.upstream_host, u.upstream_port, u.known_host_key
                    )
                )
                hosts_path.chmod(0o600)
                cps.append((
                    str(hosts_path),
                    f"{GIT_GATE_CREDS_DIR_IN_CONTAINER}/{u.name}-known_hosts",
                    f"upstream known_hosts for '{u.name}'",
                ))

        for src, dst, label in cps:
            cp_result = subprocess.run(
                ["docker", "cp", src, f"{name}:{dst}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if cp_result.returncode != 0:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                die(
                    f"failed to copy {label} into {name}: "
                    f"{cp_result.stderr.strip()}"
                )

        if subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to attach git-gate sidecar {name} to egress network "
                f"{plan.egress_network}"
            )

        if subprocess.run(
            ["docker", "start", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(f"failed to start git-gate sidecar {name}")

        return name

    def stop(self, target: str) -> None:
        """Idempotent: missing container is success. `target` is the
        container name returned by `.start`."""
        if subprocess.run(
            ["docker", "inspect", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            if subprocess.run(
                ["docker", "rm", "-f", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                warn(
                    f"failed to remove git-gate sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
