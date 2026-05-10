"""Docker helpers. Build/inspect primitives shared by the CLI."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Iterable

from .log import die, info


def require_docker() -> None:
    """Fail with an install pointer if `docker` is not on PATH."""
    if shutil.which("docker") is None:
        info("Docker is required but was not found on PATH.")
        info("macOS:  install Docker Desktop  https://docs.docker.com/desktop/install/mac-install/")
        info("Linux:  install Docker Engine   https://docs.docker.com/engine/install/")
        die("docker not found")


def require_runsc() -> None:
    """Fail with an install pointer if the `runsc` (gVisor) runtime is
    not registered with the local Docker daemon. Called when a bottle
    sets `runtime: "runsc"`."""
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "runsc" not in result.stdout:
        info("This bottle requested runtime 'runsc' but the gVisor runtime is not registered with Docker.")
        info("Install gVisor and register it with the daemon:  https://gvisor.dev/docs/user_guide/install/")
        info("On macOS, gVisor is not available natively; remove 'runtime' from the bottle or run on Linux.")
        die("runsc runtime not available")


def image_exists(ref: str) -> bool:
    return _silent_run(["docker", "image", "inspect", ref]) == 0


def container_exists(name: str) -> bool:
    """Returns True if a container (running or stopped) with the given
    name exists. Uses `docker ps -a -q -f name=^<name>$` so substring
    matches don't false-positive."""
    result = subprocess.run(
        ["docker", "ps", "-a", "-q", "-f", f"name=^{name}$"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase, non-alnum runs → '-', trimmed. Dies on empty result."""
    if not name:
        die("slugify: missing name")
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        die(f"name '{name}' produced an empty slug; use alphanumeric characters")
    return slug


def build_image(ref: str, context: str) -> None:
    """Invokes `docker build` every call. Layer cache makes no-change
    rebuilds cheap; running every time means Dockerfile edits land
    without manual `docker rmi`."""
    info(f"building image {ref} from {context} (layer cache keeps repeat builds fast)")
    subprocess.run(["docker", "build", "-t", ref, context], check=True)


_TRUST_DIALOG_NODE_SCRIPT = (
    'const fs=require("fs"),p=process.env.HOME+"/.claude.json",'
    'c=JSON.parse(fs.readFileSync(p,"utf8"));'
    'c.projects=c.projects||{};'
    'c.projects[process.env.HOME+"/workspace"]={hasTrustDialogAccepted:true};'
    'fs.writeFileSync(p,JSON.stringify(c,null,2));'
)


def build_image_with_cwd(derived: str, base: str, cwd: str) -> None:
    """Build a thin derived image that copies <cwd> into
    /home/node/workspace and adds a trust-dialog entry for it."""
    import os

    if not os.path.isdir(cwd):
        die(f"cwd not found at {cwd}")
    info(f"building image {derived} from {base} with {cwd} -> /home/node/workspace")
    dockerfile = (
        f"FROM {base}\n"
        f"COPY --chown=node:node . /home/node/workspace\n"
        f"RUN node -e '{_TRUST_DIALOG_NODE_SCRIPT}'\n"
        f"WORKDIR /home/node/workspace\n"
    )
    subprocess.run(
        ["docker", "build", "-t", derived, "-f", "-", cwd],
        input=dockerfile,
        text=True,
        check=True,
    )


def _silent_run(cmd: Iterable[str]) -> int:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
