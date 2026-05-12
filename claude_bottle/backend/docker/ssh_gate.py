"""DockerSSHGate — the Docker-specific lifecycle for the per-agent
SSH egress gate sidecar (PRD 0007). Inherits the platform-agnostic
prepare step (upstream allocation + entrypoint render) from
`SSHGate`."""

from __future__ import annotations

import os
import subprocess

from ...log import die, info, warn
from ...ssh_gate import SSHGate, SSHGatePlan


# alpine/socat pinned by digest. The image is `alpine` + `socat`
# pre-installed; PRD 0007 requires the gate image to be
# self-sufficient at boot (no apk pulls) because the agent-facing
# leg sits on the `--internal` network.
SSH_GATE_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_SSH_GATE_IMAGE",
    "alpine/socat@sha256:a26f4bcee25ad4a4096ce91e596c0a2fffcbb51f7fd198dd87a5c86eae66f0e1",
)

# In-container path the entrypoint script lands at after `docker cp`.
# Root path keeps the cp simple — no intermediate directories to
# create.
SSH_GATE_ENTRYPOINT_IN_CONTAINER = "/ssh-gate-entrypoint.sh"


def ssh_gate_container_name(slug: str) -> str:
    return f"claude-bottle-ssh-gate-{slug}"


def ssh_gate_host(slug: str) -> str:
    """The hostname the agent's ssh client should connect to. Same as
    the container name — Docker's embedded DNS resolves it on the
    `--internal` network (verified by the PRD 0007 DNS spike)."""
    return ssh_gate_container_name(slug)


class DockerSSHGate(SSHGate):
    """Brings the SSH gate sidecar up and down via Docker."""

    def start(self, plan: SSHGatePlan) -> str:
        """Boot the gate sidecar:
          1. `docker create` on the internal network with the
             canonical name, `--entrypoint /bin/sh`, and the
             in-container entrypoint path as the CMD.
          2. `docker cp` the entrypoint script in.
          3. Attach to the per-agent egress network so socat can dial
             upstream.
          4. `docker start`.
        Returns the container name (the target passed to `.stop`)."""
        if not plan.upstreams:
            die("DockerSSHGate.start called with no upstreams; caller should skip")
        if not plan.internal_network or not plan.egress_network:
            die(
                "DockerSSHGate.start: internal_network / egress_network must be "
                "populated on the plan before start"
            )
        if not plan.entrypoint_script.is_file():
            die(
                f"ssh-gate entrypoint script missing at {plan.entrypoint_script}; "
                f"SSHGate.prepare must run first"
            )

        name = ssh_gate_container_name(plan.slug)
        info(f"starting ssh-gate sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            "--entrypoint", "/bin/sh",
            SSH_GATE_IMAGE,
            SSH_GATE_ENTRYPOINT_IN_CONTAINER,
        ]
        if subprocess.run(
            create_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0:
            die(f"failed to create ssh-gate sidecar {name}")

        cp_result = subprocess.run(
            [
                "docker", "cp",
                str(plan.entrypoint_script),
                f"{name}:{SSH_GATE_ENTRYPOINT_IN_CONTAINER}",
            ],
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
                f"failed to copy ssh-gate entrypoint into {name}: "
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
                f"failed to attach ssh-gate sidecar {name} to egress network "
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
            die(f"failed to start ssh-gate sidecar {name}")

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
                    f"failed to remove ssh-gate sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
