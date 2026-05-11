"""DockerPipelockProxy — the Docker-specific implementation of the
sidecar's start/stop lifecycle. Inherits the platform-agnostic
YAML-config generation from PipelockProxy."""

from __future__ import annotations

import subprocess

from ...log import die, info, warn
from ...pipelock import (
    PIPELOCK_IMAGE,
    PIPELOCK_PORT,
    PipelockProxy,
    PipelockProxyPlan,
    pipelock_container_name,
)


class DockerPipelockProxy(PipelockProxy):
    """Brings the pipelock sidecar up and down via Docker."""

    def start(self, plan: PipelockProxyPlan) -> str:
        """Boot the pipelock sidecar:
          1. `docker create` on the internal network with the canonical
             name and argv `run --config /etc/pipelock.yaml --listen
             0.0.0.0:<port>`.
          2. `docker cp` the YAML config to /etc/pipelock.yaml in the
             writable layer (parent dir must already exist; image is
             distroless).
          3. Attach to the per-agent egress network.
          4. `docker start`.
        Returns the container name (the proxy_target passed to .stop)."""
        name = pipelock_container_name(plan.slug)
        if not plan.yaml_path.is_file():
            die(
                f"pipelock yaml not found at {plan.yaml_path}; "
                f"PipelockProxy.prepare must run first"
            )

        info(f"starting pipelock sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            PIPELOCK_IMAGE,
            "run", "--config", "/etc/pipelock.yaml",
            "--listen", f"0.0.0.0:{PIPELOCK_PORT}",
        ]
        if subprocess.run(create_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            die(f"failed to create pipelock sidecar {name}")

        cp_result = subprocess.run(
            ["docker", "cp", str(plan.yaml_path), f"{name}:/etc/pipelock.yaml"],
            capture_output=True,
            text=True,
        )
        if cp_result.returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(f"failed to copy pipelock yaml into {name}: {cp_result.stderr.strip()}")

        if subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(f"failed to attach pipelock sidecar {name} to egress network {plan.egress_network}")

        if subprocess.run(
            ["docker", "start", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode != 0:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            die(f"failed to start pipelock sidecar {name}")

        return name

    def stop(self, proxy_target: str) -> None:
        """Idempotent: missing container is success. `proxy_target` is
        the container name returned by .start."""
        if subprocess.run(
            ["docker", "inspect", proxy_target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            if subprocess.run(
                ["docker", "rm", "-f", proxy_target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode != 0:
                warn(
                    f"failed to remove pipelock sidecar {proxy_target}; "
                    f"clean up with 'docker rm -f {proxy_target}'"
                )
