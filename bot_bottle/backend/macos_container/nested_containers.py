"""Guest-local container engine for Apple-container bottles (issue #392).

The service and every nested container remain inside the existing per-bottle
VM.  This module refuses to compensate for missing prerequisites with outer
capabilities, a privileged container, or a host Docker socket.

Podman is used rather than rootless Docker for one specific reason: Apple
Container's capability bounding set omits `CAP_SYS_ADMIN`, which the kernel
requires to write a multi-range `uid_map` via `newuidmap`.  Rootless Docker
has no path that avoids that write.  Podman does — with no subordinate UID
range configured it falls back to a single-UID self-mapping, which an
unprivileged process may write itself.  See
`docs/research/rootless-docker-in-apple-container-spike.md`.

That fallback is why `build_image` *removes* the agent user's `/etc/subuid`
and `/etc/subgid` entries instead of adding them: their presence is precisely
what would send podman down the `newuidmap` path that cannot work here.

The agent still talks to `docker` and `docker compose`; those speak to
podman's Docker-compatible API socket, so nothing in the agent's habits
changes.
"""

from __future__ import annotations

import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from ...log import die, info

_INIT = "/usr/local/libexec/bot-bottle/nested-containers-init"
# Deliberately cryptic and short. podman derives conmon's attach socket as
# `$XDG_RUNTIME_DIR/libpod/tmp/socket/<64-hex-id>/attach`, and a Unix socket
# path may not exceed 108 bytes (`sun_path`). The descriptive
# `/tmp/bot-bottle-podman-run` produced a 116-byte path — over the limit, so
# attach would have broken as soon as anything got far enough to attach. Do
# not lengthen this for readability; it buys 8 bytes of headroom.
_RUNTIME_DIR = "/tmp/bbp"
_SOCKET = f"{_RUNTIME_DIR}/podman.sock"
_LOG = "/tmp/bot-bottle-nested-containers.log"
IMAGE_SUFFIX = "-nested-containers"
READY_RETRIES = 30

# Apple Container creates both device nodes 0600 root:root, so the agent user
# cannot open them: /dev/fuse blocks the fuse-overlayfs storage driver and
# /dev/net/tun blocks slirp4netns, which rootless podman uses for the default
# bridge network that stock compose files expect.  Relaxing the modes needs no
# capability the bottle does not already hold — unlike CAP_SYS_ADMIN, which is
# what killed the rootless-Docker approach.
_GUEST_DEVICES = ("/dev/fuse", "/dev/net/tun")


def build_image(
    base_image: str,
    build: Callable[..., None],
    pin_local_base: Callable[[str], str],
) -> str:
    """Layer the nested-container tooling onto an already-built agent image.

    Podman and its networking stack live here rather than in the base agent
    images so that bottles without the flag pay no image-size cost.

    # TODO(#394): replace this hand-rolled Dockerfile with a docker-layer
    # abstraction once that infrastructure exists.
    """
    image = f"{base_image}{IMAGE_SUFFIX}"
    pinned_base = pin_local_base(base_image)
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
            "RUN apt-get update \\\n"
            # podman 5's networking stack, installed explicitly because
            # --no-install-recommends omits it and each missing piece fails
            # at a different, misleading layer:
            #   podman       -> moved here from the base agent images so that
            #                   bottles without nested_containers pay no cost
            #   passt        -> `pasta`, the default rootless netns helper
            #                   (podman 4 used slirp4netns); without it
            #                   nothing starts: "could not find pasta"
            #   nftables     -> `nft`, which netavark shells out to for the
            #                   bridge network every compose file expects
            #   aardvark-dns -> name resolution *inside* nested containers;
            #                   without it DNS fails while everything else
            #                   looks healthy
            # slirp4netns stays as the documented fallback for pasta.
            " && apt-get install -y --no-install-recommends "
            "aardvark-dns fuse-overlayfs netavark nftables passt podman "
            "slirp4netns uidmap \\\n"
            " && rm -rf /var/lib/apt/lists/* \\\n"
            # Deliberate: an empty subordinate range keeps podman on the
            # single-UID mapping that needs no CAP_SYS_ADMIN.  Adding ranges
            # here would reintroduce the newuidmap failure this design exists
            # to route around.
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
    """Environment consumed by the Docker CLI inside an enabled bottle."""
    if not enabled:
        return {}
    return {
        "DOCKER_HOST": f"unix://{_SOCKET}",
        "XDG_RUNTIME_DIR": _RUNTIME_DIR,
    }


def prepare_guest_devices(container_name: str, exec_as_root: Callable[..., None]) -> None:
    """Make /dev/fuse and /dev/net/tun openable by the agent user.

    Runs as root inside the bottle because the agent must not be able to
    re-mode device nodes itself.  No outer capability is involved.
    """
    exec_as_root(
        container_name,
        ["sh", "-c", f"chmod 0666 {' '.join(_GUEST_DEVICES)}"],
    )


def start(bottle: object) -> None:
    """Start and verify the unprivileged service through the bottle exec API."""
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
