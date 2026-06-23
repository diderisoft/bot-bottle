"""SmolmachinesFreezer — snapshot a smolmachines bottle.

`smolvm pack create --from-vm` requires the VM to be stopped, and smolvm
removes VMs when stopped (same issue as Apple Container). Instead, exec
into the running VM as root to write a gzip-compressed tar of the root
filesystem to /var/tmp, then copy it to the host with `smolvm machine cp`,
build a Docker image from the archive, convert it to a smolmachine artifact
via the existing registry pipeline, and record the sidecar path. The VM
stays running throughout."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .. import ActiveAgent
from ..freeze import Freezer
from ..docker import util as docker_mod
from .local_registry import crane_push_tarball, ephemeral_registry
from .smolvm import machine_cp, machine_exec, pack_create
from ...bottle_state import bottle_state_dir
from ...log import die, info


# Temp file written inside the VM during commit. Lives in /var/tmp
# (on-disk, unlike tmpfs /tmp) to survive for machine_cp.
_VM_COMMIT_TAR = "/var/tmp/.bot-bottle-commit.tar.gz"


class SmolmachinesFreezer(Freezer):
    """Freezes a smolmachines bottle via exec-tar + Docker image + smolmachine pack.

    The VM is NOT stopped. We exec into the running VM to write a compressed
    tar of the root filesystem to /var/tmp, copy it to the host with
    machine_cp, build a Docker image (Docker's ADD decompresses .tar.gz
    automatically), then run the same image→registry→pack_create pipeline
    that _ensure_smolmachine uses for fresh builds."""

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
        # Use .tar.gz — Docker ADD decompresses automatically and the
        # compressed archive fits in the VM's /var/tmp more easily.
        rootfs_tar_gz = tmp_path / "rootfs.tar.gz"
        dockerfile = tmp_path / "Dockerfile"

        _exec_tar_to_file(machine, rootfs_tar_gz)

        dockerfile.write_text(
            "FROM scratch\n"
            "ADD rootfs.tar.gz /\n"
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
    """Snapshot the running VM's root filesystem to dest (.tar.gz).

    Writes a gzip-compressed tar to _VM_COMMIT_TAR inside the VM via
    machine_exec (same mechanism as provisioning), then copies it to the
    host with machine_cp. This avoids binary-stdout piping through the
    smolvm exec channel, which does not reliably handle large binary output.

    A connectivity probe (machine_exec true) runs first so a concurrent-exec
    limitation (smolvm may reject a second exec while -i -t is active) is
    reported clearly rather than as a silent failure."""
    # Connectivity probe — if smolvm rejects concurrent exec while an
    # interactive session is running, fail clearly here.
    probe = machine_exec(machine, ["true"])
    if probe.returncode != 0:
        die(
            f"smolvm exec is not available for {machine!r} "
            f"(exit {probe.returncode}: {probe.stderr.strip() or probe.stdout.strip() or '<no output>'}). "
            f"If an interactive session is active, smolvm may not support concurrent exec."
        )

    # Create the compressed tar inside the VM.
    # tar exits 1 when files change during archiving (normal for a live
    # filesystem); only treat exit > 1 as fatal.
    tar_result = machine_exec(
        machine,
        [
            "tar", "--create", "--gzip",
            "--exclude=./proc",
            "--exclude=./sys",
            "--exclude=./dev",
            "--exclude=./run",
            f"--file={_VM_COMMIT_TAR}",
            "--directory=/",
            ".",
        ],
    )
    if tar_result.returncode > 1:
        die(
            f"smolvm exec tar {machine!r} failed (exit {tar_result.returncode}): "
            f"{tar_result.stderr.strip() or tar_result.stdout.strip() or '<no output>'}"
        )

    # Copy from VM to host, then clean up.
    try:
        machine_cp(f"{machine}:{_VM_COMMIT_TAR}", str(dest))
    finally:
        machine_exec(machine, ["rm", "-f", _VM_COMMIT_TAR])
