"""Post-boot isolation probe for firecracker VMs, run over SSH. Listing the nft
table usually needs root, so the boundary is proven from inside. See
`backend.isolation_probe`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import isolation_probe
from . import util


def verify_isolation(private_key: Path, guest_ip: str, *, check: str = "") -> None:
    """Prove the VM can't reach the host's primary address. Dies on a leak or
    an inconclusive probe."""
    ssh = util.ssh_base_argv(private_key, guest_ip)

    def run_guest(script: str) -> str:
        return subprocess.run(
            [*ssh, "--", "sh", "-s"],
            input=script, capture_output=True, text=True, check=False,
        ).stdout

    isolation_probe.verify_isolation(
        run_guest,
        what="the VM",
        remedy="Verify the nft table with: bot-bottle install --backend=firecracker",
        check=check,
    )
