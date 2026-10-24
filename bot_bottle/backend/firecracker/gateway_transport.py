"""`GatewayTransport` for the gateway VM: exec and cp over SSH."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

from ...control.gateway import GatewayProvisionError
from . import util


class FirecrackerGatewayTransport:

    def __init__(self, private_key: Path, guest_ip: str) -> None:
        self._key = private_key
        self._ip = guest_ip

    def exec(self, argv: list[str]) -> None:
        proc = subprocess.run(
            util.ssh_base_argv(self._key, self._ip) + [shlex.join(argv)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"infra gateway exec {argv!r} failed: {proc.stderr.strip()}")

    def cp_into(self, src: str, dest: str) -> None:
        # Keep the source mode, as `docker cp` does: the access hook must stay
        # executable and keys 0600.
        mode = stat.S_IMODE(os.stat(src).st_mode)
        q = shlex.quote(dest)
        proc = subprocess.run(
            util.ssh_base_argv(self._key, self._ip)
            + [f"cat > {q} && chmod {mode:o} {q}"],
            input=Path(src).read_bytes(), capture_output=True, timeout=30, check=False,
        )
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"infra gateway cp {src} -> {dest} failed: "
                f"{proc.stderr.decode(errors='replace').strip()}")


__all__ = ["FirecrackerGatewayTransport"]
