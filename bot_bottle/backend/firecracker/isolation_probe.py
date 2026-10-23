"""Post-boot isolation probe for Firecracker VMs.

The pre-boot nft check can't be trusted on every host (listing an nftables
table typically needs root; the launcher runs unprivileged), so before the
agent runs we prove the boundary from inside the VM over SSH. See
`backend.isolation_probe` for how the check works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import isolation_probe
from . import util


def verify_isolation(private_key: Path, guest_ip: str) -> None:
    """Prove the VM cannot reach the host's primary address. Dies
    (fail-closed) on a leak or an inconclusive probe."""
    ssh = util.ssh_base_argv(private_key, guest_ip)

    def run_guest(script: str) -> str:
        return subprocess.run(
            [*ssh, "--", "sh", "-s"],
            input=script, capture_output=True, text=True, check=False,
        ).stdout

    isolation_probe.verify_isolation(
        run_guest,
        what="the VM",
        remedy="Verify the nft table with: bot-bottle backend setup --backend=firecracker",
    )
