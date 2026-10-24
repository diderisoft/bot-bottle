"""`FirecrackerFreezer`: stream a running VM's root filesystem over SSH to a
tar, the resumable artifact. `resume` rebuilds an ext4 from it. The bottle
keeps running.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...bottle_state import committed_rootfs_path
from ...lib.log import die, info
from .. import ActiveAgent
from ..freeze import Freezer
from . import util


class FirecrackerFreezer(Freezer):
    backend_name = "firecracker"

    def _freeze(self, agent: ActiveAgent) -> str:
        run_dir = util.cache_dir() / "run" / agent.slug
        private_key = run_dir / "bottle_id_ed25519"
        guest_ip = util.guest_ip_from_config(run_dir / "config.json")
        if not private_key.is_file() or not guest_ip:
            die(f"cannot freeze {agent.slug}: run dir {run_dir} is missing the "
                f"SSH key or VM config (is the bottle still running?)")
        tar_path = committed_rootfs_path(agent.slug)
        _commit_rootfs_via_ssh(private_key, guest_ip, tar_path)
        info(f"committed {agent.slug} -> {tar_path}")
        return str(tar_path)

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      cp {image_ref} {slug}.tar")


def _commit_rootfs_via_ssh(private_key: Path, guest_ip: str, tar_path: Path) -> None:
    """Stream the guest rootfs to `tar_path`, excluding proc/sys/dev/run, via a
    `.partial` renamed on success."""
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    partial = tar_path.with_name(tar_path.name + ".partial")
    ssh = util.ssh_base_argv(private_key, guest_ip)
    # The snapshot may hold the workspace: recreate the partial with
    # O_EXCL|O_NOFOLLOW at 0600 and fchmod it, then re-assert after rename.
    partial.unlink(missing_ok=True)
    fd = os.open(
        partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as tar_out:
        result = subprocess.run(
            [*ssh, "--", "tar", "--create", "--one-file-system",
             "--exclude=./proc", "--exclude=./sys", "--exclude=./dev",
             "--exclude=./run", "--file=-", "--directory=/", "."],
            stdout=tar_out, stderr=subprocess.PIPE, check=False,
        )
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        die(f"ssh tar for {guest_ip} failed: "
            f"{(result.stderr or b'').decode().strip() or '<no stderr>'}")
    os.replace(partial, tar_path)
    os.chmod(tar_path, 0o600)
