"""SmolmachinesFreezer — snapshot a smolmachines bottle.

`smolvm pack create --from-vm` requires the VM to be stopped, and smolvm
removes VMs when stopped (same issue as Apple Container). Instead, exec
into the running VM as root and stream the root filesystem via tar, build
a Docker image from the archive, convert it to a smolmachine artifact via
the existing registry pipeline, and record the sidecar path. The VM stays
running throughout."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .. import ActiveAgent
from ..freeze import Freezer
from ..docker import util as docker_mod
from .local_registry import crane_push_tarball, ephemeral_registry
from .smolvm import pack_create
from ...bottle_state import bottle_state_dir
from ...log import die, info


class SmolmachinesFreezer(Freezer):
    """Freezes a smolmachines bottle via exec-tar + Docker image + smolmachine pack.

    The VM is NOT stopped. smolvm machine exec streams the root filesystem
    via tar; we build a Docker image from it and run the same image→registry→
    pack_create pipeline that _ensure_smolmachine uses for fresh builds."""

    backend_name = "smolmachines"

    def _freeze(self, agent: ActiveAgent) -> str:
        machine = f"bot-bottle-{agent.slug}"
        image_ref = f"bot-bottle-committed-{agent.slug}:latest"
        output_dir = bottle_state_dir(agent.slug)
        output_dir.mkdir(parents=True, exist_ok=True)
        binary = output_dir / "committed-smolmachine"
        sidecar = output_dir / "committed-smolmachine.smolmachine"
        _snapshot_running_vm(machine, image_ref, binary)
        return str(sidecar)

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      cp {image_ref} {slug}.smolmachine")


def _snapshot_running_vm(machine: str, image_ref: str, binary: Path) -> None:
    """Exec-tar the running VM, build a Docker image, and pack to a smolmachine.

    binary:  destination for the launcher (sibling .smolmachine is the artifact
             that machine_create --from consumes, same convention as pack_create).
    """
    with tempfile.TemporaryDirectory(prefix="bot-bottle-vm-commit.") as tmp:
        tmp_path = Path(tmp)
        rootfs_tar = tmp_path / "rootfs.tar"
        dockerfile = tmp_path / "Dockerfile"

        _exec_tar_to_file(machine, rootfs_tar)

        dockerfile.write_text(
            "FROM scratch\n"
            "ADD rootfs.tar /\n"
            "USER node\n"
            "WORKDIR /home/node\n"
        )
        docker_mod.build_image(image_ref, str(tmp_path), dockerfile=str(dockerfile))

    image_tarball = binary.parent / "committed.image.tar"
    docker_mod.save(image_ref, str(image_tarball))
    try:
        with ephemeral_registry() as handle:
            digest = docker_mod.image_id(image_ref).split(":", 1)[-1][:16]
            push_ref = f"{handle.push_endpoint}/bot-bottle-committed:{digest}"
            pack_ref = f"{handle.pull_endpoint}/bot-bottle-committed:{digest}"
            crane_push_tarball(handle, str(image_tarball), push_ref)
            pack_create(pack_ref, binary)
    finally:
        image_tarball.unlink(missing_ok=True)


def _exec_tar_to_file(machine: str, dest: Path) -> None:
    """Stream the VM root filesystem as a tar archive to `dest`.

    smolvm machine exec requires stdout to be a pipe, not a regular file.
    We use Popen with stdout=PIPE and drain stderr in a background thread
    to avoid deadlock if either buffer fills while we're writing the other."""
    proc = subprocess.Popen(
        [
            "smolvm", "machine", "exec",
            "--name", machine, "--",
            "tar", "--create",
            "--exclude=./proc",
            "--exclude=./sys",
            "--exclude=./dev",
            "--exclude=./run",
            "--file=-",
            "--directory=/",
            ".",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        stderr_chunks.append(proc.stderr.read())

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    assert proc.stdout is not None
    with open(dest, "wb") as out:
        shutil.copyfileobj(proc.stdout, out, length=65536)

    t.join()
    returncode = proc.wait()
    stderr = b"".join(stderr_chunks).decode(errors="replace").strip()

    if returncode != 0:
        die(
            f"smolvm exec tar {machine!r} failed: "
            f"{stderr or '<no stderr>'}"
        )
