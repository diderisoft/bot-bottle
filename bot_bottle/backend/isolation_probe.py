"""Launch-time proof that an agent cannot reach the network except through
the gateway — the authoritative, fail-closed egress-boundary check.

Configuration can't be trusted to describe the boundary (a network created
by an older release, a firewall rule that didn't load, a runtime that changed
its defaults), so before the agent runs we test it from inside: open a canary
TCP listener on the host's primary address, which an isolated agent must not
be able to reach, and have the guest try to connect. A reachable canary means
the agent has a route off the box that bypasses the gateway, and we refuse to
continue.

The listener is load-bearing: without it a "blocked" result could just be
connection-refused. With it, a reachable canary means a real leak and a
timeout means a real drop.

The guest also tries a public address directly: blocked proves nothing (the
host may be offline), but a connection is an unambiguous leak, and it catches
hosts whose firewall happens to drop traffic to the canary.

Optionally the guest also tries to resolve a public name. An agent that can
resolve names without the gateway has a DNS channel out of the box (the
container runtime's own resolver forwarding queries), which can carry data
even with no TCP route.
"""

from __future__ import annotations

import re
import select
import shlex
import socket
from typing import Callable

from ..lib.log import die, info, warn

# Guest exit codes.
_LEAK = 0          # reached the canary
_BLOCKED = 1       # could not reach it (and, if checked, could not resolve)
_NO_TOOL = 2       # no python3 / bash / nc to test with
_DNS_LEAK = 3      # resolved a public name without the gateway
_PUBLIC_LEAK = 4   # connected to a public address without the gateway

_DNS_NAME = "example.com"
# A public address the agent must not reach directly. Unlike the canary this
# proves nothing when blocked (the host may be offline), but a connection to
# it is unambiguous.
_PUBLIC_IP = "1.1.1.1"

_GUEST_PROBE = r"""
# try_connect HOST PORT: 0 = connected, 1 = not, 2 = no tool to test with.
try_connect() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" "$2" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(3)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
  elif command -v bash >/dev/null 2>&1; then
    timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" >/dev/null 2>&1 || return 1
  elif command -v nc >/dev/null 2>&1; then
    nc -w3 -z "$1" "$2" >/dev/null 2>&1 || return 1
  else
    return 2
  fi
}
report() { echo "bb-isolation-probe=$1"; exit 0; }
try_connect "$h" "$p"; rc=$?
[ "$rc" -eq 0 ] && report 0
[ "$rc" -eq 2 ] && report 2
try_connect "$pub" 443 && report 4
if [ "$dns" = 1 ]; then
  if command -v getent >/dev/null 2>&1; then
    timeout 5 getent hosts "$name" >/dev/null 2>&1 && report 3
  elif command -v python3 >/dev/null 2>&1; then
    timeout 5 python3 -c 'import socket, sys; socket.getaddrinfo(sys.argv[1], 80)' \
      "$name" >/dev/null 2>&1 && report 3
  fi
fi
report 1
"""

_RESULT = re.compile(r"^bb-isolation-probe=(\d+)$", re.MULTILINE)


def guest_probe_script(canary_ip: str, canary_port: int, *, check_dns: bool) -> str:
    """The shell script the guest runs. It prints `bb-isolation-probe=<n>`:
    0 = reached the canary, 1 = blocked, 2 = no tool to test with, 3 =
    resolved a public name, 4 = reached a public address. No such line means
    the script never ran (e.g. the exec itself failed)."""
    header = (
        f"h={shlex.quote(canary_ip)}; p={canary_port}; "
        f"pub={_PUBLIC_IP}; dns={1 if check_dns else 0}; name={shlex.quote(_DNS_NAME)}\n"
    )
    return header + _GUEST_PROBE


def host_canary_ip() -> str:
    """The source address the host uses for off-box traffic (its LAN
    address) — a canary the agent must not reach. Empty if the host has no
    such route (an isolated box). Sends nothing: connecting a UDP socket only
    selects the route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))
            return str(s.getsockname()[0])
    except OSError:
        return ""


def verify_isolation(
    run_guest: Callable[[str], str],
    *,
    what: str,
    remedy: str,
    check_dns: bool = False,
) -> None:
    """Prove the agent (`what`, e.g. "the VM") cannot reach the host's
    primary address, and with `check_dns` cannot resolve a public name.
    `run_guest(script)` runs the shell script inside the agent and returns its
    stdout. Dies (fail-closed) on a confirmed leak or when the guest has no
    tool to test with; `remedy` tells the operator what to check. Warns and
    skips when the host has no address to test against."""
    canary_ip = host_canary_ip()
    if not canary_ip:
        warn("isolation probe skipped: host has no off-box route to test "
             "against (isolated box).")
        return

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((canary_ip, 0))
        listener.listen(1)
        canary_port = listener.getsockname()[1]
        out = run_guest(guest_probe_script(canary_ip, canary_port, check_dns=check_dns))
        # A connection that completed its handshake waits in the listen backlog
        # whether or not anyone accepted it, so the listener — not the guest's
        # exit code — is the authority on whether the canary was reached.
        reached, _, _ = select.select([listener], [], [], 0.5)

    match = _RESULT.search(out)
    rc = int(match.group(1)) if match else None
    if rc == _LEAK or reached:
        die(f"ISOLATION FAILURE: {what} reached the host canary "
            f"{canary_ip}:{canary_port}. The egress boundary is not in "
            f"force — refusing to run the agent (fail-closed). {remedy}")
    if rc == _PUBLIC_LEAK:
        die(f"ISOLATION FAILURE: {what} connected to {_PUBLIC_IP}:443 without "
            f"the gateway. The egress boundary is not in force — refusing to "
            f"run the agent (fail-closed). {remedy}")
    if rc == _DNS_LEAK:
        die(f"ISOLATION FAILURE: {what} resolved {_DNS_NAME} without the "
            f"gateway, so DNS is a channel out of the box. Refusing to run the "
            f"agent (fail-closed). {remedy}")
    if rc == _NO_TOOL:
        die(f"isolation probe inconclusive: {what} has no python3/bash/nc "
            f"to run the connectivity test. Refusing to continue (fail-closed).")
    if rc != _BLOCKED:
        die(f"isolation probe did not run in {what} "
            f"(result {rc if rc is not None else 'missing'}). Refusing to "
            f"continue (fail-closed).")
    info(f"isolation verified: {what} cannot reach host {canary_ip}")
