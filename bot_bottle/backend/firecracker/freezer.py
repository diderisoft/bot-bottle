"""FirecrackerFreezer — snapshot a running microVM to a Docker image.

The VM is live and can't be block-copied safely, so — like the macOS
backend — we stream the guest root filesystem out over the control
channel (SSH here) and rebuild an image from it. The bottle keeps
running after the snapshot.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

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
        image_tag = f"bot-bottle-committed-{agent.slug}:latest"
        _commit_via_ssh(private_key, guest_ip, image_tag)
        info(f"committed {agent.slug} -> {image_tag!r}")
        return image_tag

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      docker image save {image_ref} "
             f"-o {slug}.tar")


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


def _commit_via_ssh(private_key: Path, guest_ip: str, image_tag: str) -> None:
    with tempfile.TemporaryDirectory(prefix="bot-bottle-fc-commit.") as tmp:
        rootfs_tar = os.path.join(tmp, "rootfs.tar")
        ssh = util.ssh_base_argv(private_key, guest_ip)
        with open(rootfs_tar, "wb") as tar_out:
            result = subprocess.run(
                [*ssh, "--", "tar", "--create", "--one-file-system",
                 "--exclude=./proc", "--exclude=./sys", "--exclude=./dev",
                 "--exclude=./run", "--file=-", "--directory=/", "."],
                stdout=tar_out, stderr=subprocess.PIPE, check=False,
            )
        if result.returncode != 0:
            die(f"ssh tar for {guest_ip} failed: "
                f"{(result.stderr or b'').decode().strip() or '<no stderr>'}")
        with open(os.path.join(tmp, "Dockerfile"), "w", encoding="utf-8") as f:
            f.write("FROM scratch\nADD rootfs.tar /\nUSER node\nWORKDIR /home/node\n")
        build = subprocess.run(
            ["docker", "build", "-t", image_tag, tmp], check=False,
        )
        if build.returncode != 0:
            die(f"docker build for {image_tag!r} failed")
