"""Firecracker's primitives for reattaching running agent VMs to the gateway
(PRD 0081); the flow itself is `backend.gateway_attach`.

Live agent VMs are their run dirs: each holds the VM's Firecracker config (its
guest IP is on the kernel command line) and the per-bottle SSH key the host
reaches it with. Commands run as the guest's root through dropbear, which uses
root's own PATH, not the agent's.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..gateway_attach import GatewayAttachError, GatewayAttacher, RunningBottle
from ..util import install_agent_ca_script
from . import cleanup, util

_SSH_TIMEOUT_SECONDS = 30


class FirecrackerGatewayAttacher(GatewayAttacher):
    def running_bottles(self) -> list[RunningBottle]:
        bottles: list[RunningBottle] = []
        for run_dir in cleanup.live_run_dirs():
            if not (run_dir / "bottle_id_ed25519").is_file():
                raise GatewayAttachError(
                    f"agent VM {run_dir.name} is running but its SSH key is missing "
                    f"from {run_dir}"
                )
            bottles.append(RunningBottle(
                ref=str(run_dir), slug=run_dir.name,
                source_ip=util.guest_ip_from_config(run_dir / "config.json"),
            ))
        return bottles

    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        self._ssh(bottle, install_agent_ca_script("-"), stdin=ca_pem,
                  what="installing the gateway CA")

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        return self._ssh(
            bottle, f"cat {util.ENV_VAR_SECRET_PATH} 2>/dev/null || true",
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
                f"{_printable(proc.stderr) or '<no stderr>'}"
            )
        return proc.stdout


def _printable(text: str) -> str:
    """Agent-controlled output, minus control characters (no terminal escapes)."""
    return "".join(c for c in text.strip() if c.isprintable() or c == "\n")


__all__ = ["FirecrackerGatewayAttacher"]
