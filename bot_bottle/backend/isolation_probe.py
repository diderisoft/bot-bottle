"""Launch-time, fail-closed proof that an agent can't reach the network except
through the gateway.

The guest tries to connect to a canary TCP listener on the host's primary
address and to a public address, and optionally to resolve a public name. The
listener makes a block a real drop rather than connection-refused. Any success
is a leak and the launch is refused.
"""

from __future__ import annotations

import re
import select
import shlex
import socket
from typing import Callable

from ..lib.log import die, info, warn

# Guest exit codes.
_LEAK = 0  # reached the canary
_BLOCKED = 1  # blocked (and, if checked, couldn't resolve)
_NO_TOOL = 2  # no python3 / bash / nc to test with
_DNS_LEAK = 3  # resolved a public name without the gateway
_PUBLIC_LEAK = 4  # connected to a public address without the gateway

_DNS_NAME = "example.com"
# A public address the agent must not reach directly. Blocked proves nothing;
# connected is a leak.
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
[ "$rc" -eq 2 ] && report 2
# With the host check warn-only, keep going so the other checks still run.
[ "$rc" -eq 0 ] && [ "$eh" = 1 ] && report 0
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


def guest_probe_script(
    canary_ip: str, canary_port: int, *, check_dns: bool, enforce_host: bool = True,
) -> str:
    """The guest script. It prints `bb-isolation-probe=<n>` (an exit code
    above); no such line means it never ran."""
    header = (
        f"h={shlex.quote(canary_ip)}; p={canary_port}; "
        f"pub={_PUBLIC_IP}; dns={1 if check_dns else 0}; name={shlex.quote(_DNS_NAME)}; "
        f"eh={1 if enforce_host else 0}\n"
    )
    return header + _GUEST_PROBE


def host_canary_ip() -> str:
    """The host's source address for off-box traffic, the canary target, or ""
    if it has no such route. Sends nothing."""
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
    host_gap: str = "",
) -> None:
    """Prove the agent (`what`) can't reach the host's primary address or a
    public address, and with `check_dns` can't resolve a public name.
    `run_guest(script)` returns the guest's stdout. Dies on a leak or a missing
    tool, with `remedy`; warns and skips when the host has no address to test.

    A non-empty `host_gap` makes reaching the host a warning that cites it,
    for a backend with a known, tracked gap there; the other checks still
    fail closed."""
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
        out = run_guest(guest_probe_script(
            canary_ip, canary_port, check_dns=check_dns, enforce_host=not host_gap))
        # A completed handshake sits in the backlog whether or not it was
        # accepted, so the listener is the authority on whether the canary was
        # reached.
        reached, _, _ = select.select([listener], [], [], 0.5)

    match = _RESULT.search(out)
    rc = int(match.group(1)) if match else None
    if not host_gap and (rc == _LEAK or reached):
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
    if reached:
        warn(f"isolation gap: {what} can reach the host at {canary_ip}; "
             f"public addresses and DNS are blocked. {host_gap}")
        return
    info(f"isolation verified: {what} cannot reach host {canary_ip}")
