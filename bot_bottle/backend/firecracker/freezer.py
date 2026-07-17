"""FirecrackerFreezer — snapshot a running microVM to a rootfs tar.

The VM is live and can't be block-copied safely, so — like the macOS
backend — we stream the guest root filesystem out over the control
channel (SSH here). Unlike the other backends this needs no Docker: the
tar *is* the resumable artifact. `resume` extracts it and rebuilds a
fresh per-bottle ext4 with `mke2fs -d` (see `util.build_committed_rootfs_dir`
and `launch._build_agent_base`). The bottle keeps running after the
snapshot.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ...bottle_state import committed_rootfs_path
from ...log import die, info
from .. import ActiveAgent
from ..freeze import Freezer
from . import util


class FirecrackerFreezer(Freezer):
    backend_name = "firecracker"

    def _freeze(self, agent: ActiveAgent) -> str:
        run_dir = util.cache_dir() / "run" / agent.slug
        private_key = run_dir / "bottle_id_ed25519"
        guest_ip = _guest_ip_from_config(run_dir / "config.json")
        if not private_key.is_file() or not guest_ip:
            die(f"cannot freeze {agent.slug}: run dir {run_dir} is missing the "
                f"SSH key or VM config (is the bottle still running?)")
        tar_path = committed_rootfs_path(agent.slug)
        _commit_rootfs_via_ssh(private_key, guest_ip, tar_path)
        info(f"committed {agent.slug} -> {tar_path}")
        return str(tar_path)

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      cp {image_ref} {slug}.tar")


def _guest_ip_from_config(config_path: Path) -> str:
    try:
        cfg = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    boot_args = cfg.get("boot-source", {}).get("boot_args", "")
    for token in boot_args.split():
        if token.startswith("ip="):
            # ip=<client>::<gw>:<mask>::<dev>:off
            return token[len("ip="):].split(":", 1)[0]
    return ""


def _commit_rootfs_via_ssh(private_key: Path, guest_ip: str, tar_path: Path) -> None:
    """Stream the guest rootfs out over SSH into `tar_path`. Excludes the
    virtual/live mounts (proc/sys/dev/run) — resume recreates those empty
    mount points. Written to a `.partial` sibling and renamed on success so
    a failed freeze never leaves a truncated artifact in its place."""
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    partial = tar_path.with_name(tar_path.name + ".partial")
    ssh = util.ssh_base_argv(private_key, guest_ip)
    # The snapshot can contain the bottle's private workspace, so keep it
    # owner-only (0600) for the whole stream. The `os.open` mode only applies
    # on *creation*, so unlink any leftover partial (a prior interrupted run
    # could have left it world-readable, or something could swap in a symlink
    # at this predictable name) and exclusively recreate it — O_EXCL|O_NOFOLLOW
    # — then fchmod immediately so umask can't loosen it. Re-assert after the
    # rename too (os.replace carries the source mode, but be explicit).
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
