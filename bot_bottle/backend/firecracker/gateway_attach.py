"""Firecracker's primitives for reattaching running agent VMs to the gateway
(PRD 0081); the flow itself is `backend.gateway_attach`.

Live agent VMs are their run dirs: each holds the VM's Firecracker config (its
guest IP is on the kernel command line) and the per-bottle SSH key the host
reaches it with.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import AGENT_CA_PATH
from . import cleanup, util

# Where `persist_env_var_secret` writes the key on the agent VM.
ENV_VAR_SECRET_PATH = "/run/bot-bottle/env-var-secret"
_SSH_TIMEOUT_SECONDS = 30


def guest_ip_from_config(config_path: Path) -> str:
    """The agent VM's guest IP from its Firecracker config, or ""."""
    try:
        config = json.loads(config_path.read_text())
        args = config["boot-source"]["boot_args"]
        ip_arg = next(p for p in args.split() if p.startswith("ip="))
        return ip_arg.removeprefix("ip=").split(":", 1)[0]
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return ""


class FirecrackerGatewayAttacher(GatewayAttacher):
    def running_bottles(self) -> list[RunningBottle]:
        bottles: list[RunningBottle] = []
        for run_dir in cleanup.live_run_dirs():
            guest_ip = guest_ip_from_config(run_dir / "config.json")
            if not guest_ip or not (run_dir / "bottle_id_ed25519").is_file():
                raise GatewayAttachError(
                    f"agent VM {run_dir.name} is running but its guest IP or SSH "
                    f"key is missing from {run_dir}"
                )
            bottles.append(RunningBottle(ref=str(run_dir), slug=run_dir.name, source_ip=guest_ip))
        return bottles

    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        # Runs as the guest's root (dropbear accepts root); the dir may not
        # survive every rootfs build, so create it.
        self._ssh(
            bottle,
            f"mkdir -p /usr/local/share/ca-certificates && cat > {AGENT_CA_PATH} && "
            f"chmod 644 {AGENT_CA_PATH} && update-ca-certificates >/dev/null",
            stdin=ca_pem, what="installing the gateway CA",
        )

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        return self._ssh(
            bottle, f"cat {ENV_VAR_SECRET_PATH} 2>/dev/null || true",
            what="reading ENV_VAR_SECRET",
        ).strip()

    @staticmethod
    def _ssh(bottle: RunningBottle, script: str, *, what: str, stdin: str = "") -> str:
        key = Path(bottle.ref) / "bottle_id_ed25519"
        try:
            proc = subprocess.run(
                util.ssh_base_argv(key, bottle.source_ip) + [script],
                input=stdin, capture_output=True, text=True,
                timeout=_SSH_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise GatewayAttachError(f"{what} timed out after {e.timeout:.0f}s") from e
        if proc.returncode != 0:
            raise GatewayAttachError(
                f"{what} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or '<no stderr>'}"
            )
        return proc.stdout


__all__ = ["ENV_VAR_SECRET_PATH", "FirecrackerGatewayAttacher", "guest_ip_from_config"]
