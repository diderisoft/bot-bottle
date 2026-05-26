"""DockerPipelockProxy — the Docker-specific implementation of the
sidecar's start/stop lifecycle. Inherits the platform-agnostic
YAML-config generation from PipelockProxy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...log import die, info, warn
from ...pipelock import PipelockProxy, PipelockProxyPlan


# Pipelock image, pinned by digest. The digest is the multi-arch image
# index for ghcr.io/luckypipewrench/pipelock:2.3.0.
PIPELOCK_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_PIPELOCK_IMAGE",
    "ghcr.io/luckypipewrench/pipelock@sha256:3b1a39417b98406ddc5dc2d8fcb42865ddc0c68a43d355db55f0f8cb06bc6de9",
)

# Listening port for pipelock's forward proxy.
PIPELOCK_PORT = os.environ.get("CLAUDE_BOTTLE_PIPELOCK_PORT", "8888")

# In-container paths where the per-bottle CA cert + key land after
# `docker cp` in `DockerPipelockProxy.start`. Pipelock's rendered
# YAML references these paths under `tls_interception`.
PIPELOCK_CA_CERT_IN_CONTAINER = "/etc/pipelock-ca.pem"
PIPELOCK_CA_KEY_IN_CONTAINER = "/etc/pipelock-ca-key.pem"


def pipelock_container_name(slug: str) -> str:
    return f"claude-bottle-pipelock-{slug}"


def pipelock_proxy_url(slug: str) -> str:
    return f"http://{pipelock_container_name(slug)}:{PIPELOCK_PORT}"


def pipelock_tls_init(stage_dir: Path) -> tuple[Path, Path]:
    """Generate a fresh per-bottle CA via a one-shot pipelock container.

    Runs `pipelock tls init` against a host-mounted scratch dir, leaving
    `ca.pem` (public cert, mode 600) and `ca-key.pem` (private key, mode
    600) under `<stage_dir>/pipelock-ca/`. Returns the two host paths.

    The image is pinned (same digest the running sidecar uses) so the
    generated CA matches what the sidecar expects. Output is owned by
    whatever UID the one-shot ran as; `DockerPipelockProxy.start`
    `docker cp`s the files into the sidecar's filesystem layer, so
    runtime ownership inside the sidecar (root in pipelock's
    distroless image) is independent."""
    work = stage_dir / "pipelock-ca"
    work.mkdir(exist_ok=True)
    result = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{work}:/h",
         "-e", "PIPELOCK_HOME=/h",
         PIPELOCK_IMAGE, "tls", "init"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(f"pipelock tls init failed: {result.stderr.strip()}")
    cert = work / "ca.pem"
    key = work / "ca-key.pem"
    if not cert.is_file() or not key.is_file():
        die(f"pipelock tls init did not produce ca files in {work}")
    # Explicit perms in case a future pipelock release changes
    # defaults. Pipelock runs as root in its distroless image and
    # bind-mounts work with 0o600 (root reads everything); the key
    # has no reason to be readable to anyone else on the host.
    key.chmod(0o600)
    cert.chmod(0o644)
    return (cert, key)


class DockerPipelockProxy(PipelockProxy):
    """Brings the pipelock sidecar up and down via Docker."""

    CA_CERT_IN_CONTAINER = PIPELOCK_CA_CERT_IN_CONTAINER
    CA_KEY_IN_CONTAINER = PIPELOCK_CA_KEY_IN_CONTAINER

    def start(self, plan: PipelockProxyPlan) -> str:
        """Boot the pipelock sidecar:
          1. `docker create` on the internal network with the canonical
             name and argv `run --config /etc/pipelock.yaml --listen
             0.0.0.0:<port>`.
          2. `docker cp` the YAML config to /etc/pipelock.yaml.
          3. `docker cp` the CA cert + key to /etc/pipelock-ca.pem
             and /etc/pipelock-ca-key.pem (pipelock runs as root in
             its distroless image, so no chown is needed).
          4. Attach to the per-agent egress network.
          5. `docker start`.
        Returns the container name (the proxy_target passed to .stop)."""
        name = pipelock_container_name(plan.slug)
        if not plan.yaml_path.is_file():
            die(
                f"pipelock yaml not found at {plan.yaml_path}; "
                f"PipelockProxy.prepare must run first"
            )
        if not plan.ca_cert_host_path.is_file() or not plan.ca_key_host_path.is_file():
            die(
                f"pipelock CA missing at {plan.ca_cert_host_path} / "
                f"{plan.ca_key_host_path}; pipelock_tls_init must run first"
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
        create_result = subprocess.run(
            create_args, capture_output=True, text=True, check=False,
        )
        if create_result.returncode != 0:
            die(
                f"failed to create pipelock sidecar {name}: "
                f"{create_result.stderr.strip()}"
            )

        for src, dst, label in (
            (plan.yaml_path, "/etc/pipelock.yaml", "yaml"),
            (plan.ca_cert_host_path, PIPELOCK_CA_CERT_IN_CONTAINER, "ca cert"),
            (plan.ca_key_host_path, PIPELOCK_CA_KEY_IN_CONTAINER, "ca key"),
        ):
            cp_result = subprocess.run(
                ["docker", "cp", str(src), f"{name}:{dst}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if cp_result.returncode != 0:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
                die(f"failed to copy pipelock {label} into {name}: {cp_result.stderr.strip()}")

        connect_result = subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            capture_output=True, text=True, check=False,
        )
        if connect_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            die(
                f"failed to attach pipelock sidecar {name} to egress network "
                f"{plan.egress_network}: {connect_result.stderr.strip()}"
            )

        start_result = subprocess.run(
            ["docker", "start", name], capture_output=True, text=True, check=False,
        )
        if start_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            die(
                f"failed to start pipelock sidecar {name}: "
                f"{start_result.stderr.strip()}"
            )

        return name

    def stop(self, proxy_target: str) -> None:
        """Idempotent: missing container is success. `proxy_target` is
        the container name returned by .start."""
        if subprocess.run(
            ["docker", "inspect", proxy_target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            if subprocess.run(
                ["docker", "rm", "-f", proxy_target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                warn(
                    f"failed to remove pipelock sidecar {proxy_target}; "
                    f"clean up with 'docker rm -f {proxy_target}'"
                )
