"""Guest-local container engine for Apple-container bottles. Everything stays
in the bottle's VM: no outer capabilities, privileged container or host socket.

Podman, not rootless Docker: Apple's bounding set lacks `CAP_SYS_ADMIN`, needed
to write a multi-range `uid_map`. With no subordinate ranges, podman falls back
to a single-UID self-mapping, so `build_image` removes the agent's
`/etc/subuid` and `/etc/subgid` entries. See
the rootless-docker-in-apple-container-spike research note (design repo). The agent still
uses `docker` and `docker compose` against podman's API socket.
"""

from __future__ import annotations

import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from ...lib.log import die, info

_INIT = "/usr/local/libexec/bot-bottle/nested-containers-init"
# Short on purpose: conmon's attach socket path must fit in 108 bytes
# (`sun_path`).
_RUNTIME_DIR = "/tmp/bbp"
_SOCKET = f"{_RUNTIME_DIR}/podman.sock"
_LOG = "/tmp/bot-bottle-nested-containers.log"
IMAGE_SUFFIX = "-nested-containers"
READY_RETRIES = 30

# Apple creates these 0600 root, blocking fuse-overlayfs and slirp4netns.
# Relaxing the modes needs no extra capability.
_GUEST_DEVICES = ("/dev/fuse", "/dev/net/tun")


def build_image(
    pinned_base: str,
    build: Callable[..., None],
) -> str:
    """Layer the nested-container tooling onto a built agent image."""
    image = f"{pinned_base}{IMAGE_SUFFIX}"
    init_script = Path(__file__).with_name("nested-containers-init.sh")
    with tempfile.TemporaryDirectory(prefix="bot-bottle-nested-containers.") as tmp:
        context = Path(tmp)
        shutil.copy2(init_script, context / "nested-containers-init.sh")
        (context / "Dockerfile").write_text(
            "ARG DOCKER_CLI_BASE_IMAGE\n"
            "FROM ${DOCKER_CLI_BASE_IMAGE} AS docker_cli\n"
            f"FROM {pinned_base}\n"
            "USER root\n"
            "COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker\n"
            "COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/"
            "docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose\n"
            # The base images pin apt to a snapshot.debian.org date, whose
            # Release files expire; match their flag.
            "RUN apt-get -o Acquire::Check-Valid-Until=false update \\\n"
            # podman 5's networking, installed explicitly
            # (`--no-install-recommends` omits it):
            #   podman       moved here from the base images
            #   passt        `pasta`, the default rootless netns helper
            #   nftables     `nft`, which netavark uses for compose's bridge network
            #   aardvark-dns DNS inside nested containers
            # slirp4netns stays as pasta's fallback.
            " && apt-get install -y --no-install-recommends "
            "aardvark-dns fuse-overlayfs netavark nftables passt podman "
            "slirp4netns uidmap \\\n"
            " && rm -rf /var/lib/apt/lists/* \\\n"
            # An empty subordinate range keeps podman on the single-UID
            # mapping.
            " && sed -i '/^node:/d' /etc/subuid /etc/subgid\n"
            "COPY nested-containers-init.sh "
            "/usr/local/libexec/bot-bottle/nested-containers-init\n"
            "RUN chmod 0755 /usr/local/libexec/bot-bottle/nested-containers-init\n"
            "USER node\n",
            encoding="utf-8",
        )
        build(image, str(context), dockerfile=str(context / "Dockerfile"))
    return image


def guest_env(enabled: bool) -> dict[str, str]:
    """Env for the Docker CLI in an enabled bottle."""
    if not enabled:
        return {}
    return {
        "DOCKER_HOST": f"unix://{_SOCKET}",
        "XDG_RUNTIME_DIR": _RUNTIME_DIR,
    }


def prepare_guest_devices(container_name: str, exec_as_root: Callable[..., object]) -> None:
    """Make /dev/fuse and /dev/net/tun openable by the agent user. Runs as root
    in the bottle.
    """
    exec_as_root(
        container_name,
        ["sh", "-c", f"chmod 0666 {' '.join(_GUEST_DEVICES)}"],
    )


def start(bottle: object) -> None:
    """Start and verify the unprivileged service via the bottle's exec API."""
    info("starting guest-local container engine")
    result = bottle.exec(shlex.quote(_INIT))  # type: ignore[attr-defined]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(f"nested-container bootstrap failed: {detail or '<no output>'}")

    for _ in range(READY_RETRIES):
        result = bottle.exec("docker info >/dev/null 2>&1")  # type: ignore[attr-defined]
        if result.returncode == 0:
            info("guest-local container engine is ready")
            return
        time.sleep(0.2)

    logs = bottle.exec(  # type: ignore[attr-defined]
        f"tail -n 80 {_LOG} 2>/dev/null || true"
    )
    die(
        "guest-local container engine did not become ready without additional "
        f"outer privileges:\n{(logs.stdout or logs.stderr or '<no log>').strip()}"
    )


__all__ = ["build_image", "guest_env", "prepare_guest_devices", "start"]
