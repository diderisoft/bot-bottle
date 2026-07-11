"""Empirical, post-boot isolation probe — the authoritative fail-closed
egress-boundary check.

The pre-boot nft check can't be trusted on every host (listing an
nftables table typically needs root; the launcher runs unprivileged).
So before the agent ever runs, we prove the boundary directly: open a
canary TCP listener on a host IP the VM must NOT be able to reach (the
host's primary non-TAP address), then have the guest try to connect. If
the isolation rules are in force the connection is dropped and times
out; if it succeeds, the VM can reach host services it shouldn't — a
sandbox escape — and we refuse to continue.

The listener is load-bearing: without it a "blocked" result could just
be connection-refused. With it, a reachable canary means a real leak
and a timeout means a real drop.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path

from ...log import die, info, warn
from . import util


def _host_primary_ip() -> str:
    """The source IP the host uses for off-box traffic (its LAN
    address) — a canary the VM must not reach. Empty if the host has no
    such route (isolated box)."""
    result = subprocess.run(
        ["ip", "-o", "route", "get", "1.1.1.1"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return ""
    tokens = result.stdout.split()
    # `... src <addr> ...` — the address the host would use as source.
    if "src" in tokens:
        idx = tokens.index("src")
        if idx + 1 < len(tokens):
            return tokens[idx + 1]
    return ""


# Guest-side connect test: exit 0 = reached the canary (LEAK), 1 =
# blocked (good), 2 = no usable tool (inconclusive -> fail-closed).
_GUEST_PROBE = r"""
h="$1"; p="$2"
if command -v python3 >/dev/null 2>&1; then
  python3 - "$h" "$p" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(3)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
elif command -v bash >/dev/null 2>&1; then
  timeout 3 bash -c "exec 3<>/dev/tcp/$h/$p" >/dev/null 2>&1
elif command -v nc >/dev/null 2>&1; then
  nc -w3 -z "$h" "$p" >/dev/null 2>&1
else
  exit 2
fi
"""


def verify_isolation(private_key: Path, guest_ip: str) -> None:
    """Prove the VM cannot reach the host's primary IP. Dies
    (fail-closed) on a confirmed leak or if the guest has no tool to
    run the test. Warns (does not fail) when the host has no canary
    address to test against."""
    canary_ip = _host_primary_ip()
    if not canary_ip:
        warn("isolation probe skipped: host has no non-TAP route to test "
             "against (isolated box).")
        return

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((canary_ip, 0))
    listener.listen(1)
    listener.settimeout(6)
    canary_port = listener.getsockname()[1]

    accepted: list[bool] = []

    def _accept() -> None:
        try:
            conn, _ = listener.accept()
            accepted.append(True)
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()

    ssh = util.ssh_base_argv(private_key, guest_ip)
    result = subprocess.run(
        [*ssh, "--", "sh", "-s", "--", canary_ip, str(canary_port)],
        input=_GUEST_PROBE, capture_output=True, text=True, check=False,
    )
    thread.join(timeout=7)
    listener.close()

    if result.returncode == 0 or accepted:
        die(f"ISOLATION FAILURE: the VM reached the host canary "
            f"{canary_ip}:{canary_port}. The egress boundary is not in "
            f"force — refusing to run the agent (fail-closed). Verify the "
            f"nft table with: ./cli.py firecracker setup")
    if result.returncode == 2:
        die("isolation probe inconclusive: the guest has no python3/bash/nc "
            "to run the connectivity test. Refusing to continue "
            "(fail-closed).")
    info(f"isolation verified: VM cannot reach host {canary_ip}")
