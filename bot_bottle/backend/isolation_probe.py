"""Launch-time, fail-closed proof that an agent can't reach the network except
through the gateway.

The guest tries to connect to a canary TCP listener on the host's primary
address and to a public address, and optionally to resolve a public name. The
listener makes a block a real drop rather than connection-refused. Any success
is a leak and the launch is refused.

It also knocks on sibling agents' addresses (`peers`) at a port nothing
serves. With no listener there, any answer counts: a connection or a refusal
both mean the packet reached the sibling. Only silence (a drop, or no route)
passes.
"""

from __future__ import annotations

import contextlib
import re
import select
import shlex
import socket
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, cast

from ..lib.db_store import MACHINE_SPECIFIC, DbStore
from ..lib.log import die, info, warn
from ..lib.migrations import TableMigrations

# `BOT_BOTTLE_ISOLATION_CHECK` (`OperationSettings.isolation_check`; "" is
# `once`): `always` probes on every launch; `once` skips a probe of host-wide
# state (`once_key`) already passed for that same state; `off` skips them all.
ALWAYS, ONCE, OFF = "always", "once", "off"
CHECKS = (ALWAYS, ONCE, OFF)
# Host-wide keys a probe passed for (`isolation_passes`, in the host DB),
# newest last; a few, for a host that moves between networks.
_KEPT_KEYS = 8

# Guest exit codes.
_LEAK = 0  # reached the canary
_BLOCKED = 1  # blocked (and, if checked, couldn't resolve)
_NO_TOOL = 2  # no python3 / bash / nc to test with
_DNS_LEAK = 3  # resolved a public name without the gateway
_PUBLIC_LEAK = 4  # connected to a public address without the gateway
_PEER_LEAK = 5  # a sibling agent answered (connected or refused)

_DNS_NAME = "example.com"
# A public address the agent must not reach directly. Blocked proves nothing;
# connected is a leak.
_PUBLIC_IP = "1.1.1.1"
# The port knocked on at sibling agents: tcpmux, which nothing serves, so a
# reachable sibling refuses it.
PEER_PORT = 1
# At most this many siblings are knocked on; each dropped knock costs its
# 2 s timeout.
MAX_PEERS = 4

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
# try_peer HOST PORT: 0 = it answered (connected or refused), 1 = silence,
# 2 = no tool that tells a refusal from a drop.
try_peer() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" "$2" <<'PY'
import errno, socket, sys
s = socket.socket(); s.settimeout(2)
rc = s.connect_ex((sys.argv[1], int(sys.argv[2])))
sys.exit(0 if rc in (0, errno.ECONNREFUSED) else 1)
PY
  elif command -v bash >/dev/null 2>&1; then
    err=$(timeout 2 bash -c "exec 3<>/dev/tcp/$1/$2" 2>&1) && return 0
    case "$err" in *refused*) return 0 ;; esac
    return 1
  else
    return 2
  fi
}
report() { echo "bb-isolation-probe=$1"; exit 0; }
# With python3, every check at once: a blocked attempt is a drop that waits
# out its timeout, so one after another they add up (~14 s behind pf).
# The verdicts and their order are the serial path's below.
if command -v python3 >/dev/null 2>&1; then
  python3 - "$h:$p $more" "$peers" "$pub" "$dns" "$name" <<'PY'
import errno, socket, sys, threading, time
hosts, peers = sys.argv[1].split(), sys.argv[2].split()
pub, dns, name = sys.argv[3], sys.argv[4] == "1", sys.argv[5]
got = {}
def connect(key, target, timeout):
    host, _, port = target.rpartition(":")
    s = socket.socket(); s.settimeout(timeout)
    try:
        got[key] = s.connect_ex((host, int(port)))
    except OSError as e:
        got[key] = e.errno or -1
    finally:
        s.close()
def resolve():
    try:
        socket.getaddrinfo(name, 80)
        got["dns"] = 0
    except OSError:
        got["dns"] = 1
jobs = [(threading.Thread(target=connect, args=(("host", c), c, 3), daemon=True), 3.5)
        for c in hosts]
jobs += [(threading.Thread(target=connect, args=(("peer", c), c, 2), daemon=True), 2.5)
         for c in peers]
jobs.append((threading.Thread(target=connect, args=("pub", pub + ":443", 3), daemon=True), 3.5))
if dns:
    jobs.append((threading.Thread(target=resolve, daemon=True), 5))
start = time.monotonic()
for t, _ in jobs:
    t.start()
for t, limit in jobs:  # all together: this waits as long as the slowest
    t.join(max(0.0, start + limit - time.monotonic()))
def report(n):
    print(f"bb-isolation-probe={n}"); sys.exit(0)
if any(got.get(("host", c)) == 0 for c in hosts):
    report(0)
for c in peers:
    if got.get(("peer", c)) in (0, errno.ECONNREFUSED):
        print(f"bb-isolation-peer={c.rpartition(':')[0]}"); report(5)
if got.get("pub") == 0:
    report(4)
if got.get("dns") == 0:
    report(3)
report(1)
PY
  exit 0
fi
try_connect "$h" "$p"; rc=$?
[ "$rc" -eq 0 ] && report 0
[ "$rc" -eq 2 ] && report 2
for c in $more; do
  try_connect "${c%:*}" "${c##*:}" && report 0
done
for c in $peers; do
  try_peer "${c%:*}" "${c##*:}"; rc=$?
  [ "$rc" -eq 0 ] && { echo "bb-isolation-peer=${c%:*}"; report 5; }
  [ "$rc" -eq 2 ] && report 2
done
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
_PEER = re.compile(r"^bb-isolation-peer=(\S+)$", re.MULTILINE)


def guest_probe_script(canary_ip: str, canary_port: int, *, check_dns: bool,
                       more: Sequence[tuple[str, int]] = (),
                       peers: Sequence[str] = ()) -> str:
    """The guest script. It prints `bb-isolation-probe=<n>` (an exit code
    above); no such line means it never ran. `more` are further host
    canaries, IPv4 `(ip, port)`s, tried after the first. `peers` are sibling
    agents' IPv4 addresses, knocked on at `PEER_PORT`."""
    extra = " ".join(f"{ip}:{port}" for ip, port in more)
    knocks = " ".join(f"{ip}:{PEER_PORT}" for ip in peers)
    header = (
        f"h={shlex.quote(canary_ip)}; p={canary_port}; more={shlex.quote(extra)}; "
        f"peers={shlex.quote(knocks)}; "
        f"pub={_PUBLIC_IP}; dns={1 if check_dns else 0}; name={shlex.quote(_DNS_NAME)}\n"
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


class IsolationPasses(DbStore):
    """`isolation_passes`: the host-wide keys a probe passed for, a cache the
    host controller keeps (launches run there). Machine-specific."""

    TABLES = {"isolation_passes": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("isolation_passes", [
            """
            CREATE TABLE isolation_passes (
                key TEXT PRIMARY KEY,
                passed INTEGER NOT NULL
            )
            """,
        ]))
        self.migrate()

    def keys(self) -> list[str]:
        """Newest last."""
        with self._connection() as conn:
            return [r[0] for r in conn.execute("SELECT key FROM isolation_passes "
                                               "ORDER BY passed, rowid")]

    def add(self, key: str) -> None:
        """Remember `key`, keeping the newest `_KEPT_KEYS`."""
        with self._write_connection() as conn:
            newest = conn.execute("SELECT COALESCE(MAX(passed), 0) FROM isolation_passes"
                                  ).fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO isolation_passes VALUES (?, ?)",
                         (key, newest + 1))
            conn.execute("DELETE FROM isolation_passes WHERE key NOT IN (SELECT key FROM "
                         "isolation_passes ORDER BY passed DESC LIMIT ?)", (_KEPT_KEYS,))
        self._chmod()


def _passes() -> IsolationPasses | None:
    """The kept passes, in the host controller; elsewhere none are kept, so
    a probe always runs."""
    # pylint: disable=import-outside-toplevel
    from .. import host_access
    from ..paths import host_db_path
    return IsolationPasses(host_db_path()) if host_access.in_host_controller() else None


def _passed(key: str) -> bool:
    passes = _passes()
    return passes is not None and key in passes.keys()


def _remember(key: str) -> None:
    passes = _passes()
    if passes is None:
        return
    try:
        passes.add(key)
    except sqlite3.Error as e:
        warn(f"isolation probe: couldn't record the pass ({e}); it runs again next launch")


def verify_isolation(
    run_guest: Callable[[str], str],
    *,
    what: str,
    remedy: str,
    check_dns: bool = False,
    host_ips: Sequence[str] = (),
    peers: Sequence[str] = (),
    check: str = "",
    once_key: str = "",
) -> None:
    """Prove the agent (`what`) can't reach the host's primary address, the
    backend's other `host_ips` (e.g. the host's address on the agent network),
    the sibling agents at `peers` (the first `MAX_PEERS` of them) or a public
    address, and with `check_dns` can't resolve a public name.
    `run_guest(script)` returns the guest's stdout. Dies on a leak or a missing
    tool, with `remedy`; warns and skips when the host has no address to test.

    `check` is the `BOT_BOTTLE_ISOLATION_CHECK` setting. `once_key` names the
    host-wide state the probe proves (a host firewall, say): with `once`, a
    key that already passed skips the probe."""
    mode = check or ONCE
    if mode == OFF:
        warn(f"isolation probe skipped for {what}: BOT_BOTTLE_ISOLATION_CHECK=off, "
             "so nothing proves it can reach only the gateway")
        return
    peers = sorted(set(peers))[:MAX_PEERS]
    canary_ip = host_canary_ip()
    if not canary_ip:
        warn("isolation probe skipped: host has no off-box route to test "
             "against (isolated box).")
        return
    key = f"{once_key}|{canary_ip}" if once_key else ""
    if key and mode == ONCE and _passed(key):
        info(f"isolation: {what}: the host firewall passed this probe already, "
             "unchanged since; not probed again (BOT_BOTTLE_ISOLATION_CHECK=always "
             "probes every launch)")
        return

    with contextlib.ExitStack() as stack:
        listeners = []
        for ip in (canary_ip, *[h for h in host_ips if h != canary_ip]):
            listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((ip, 0))
            listener.listen(1)
            listeners.append(listener)
        canaries = [cast(tuple[str, int], ln.getsockname()[:2]) for ln in listeners]
        canary_port = canaries[0][1]
        out = run_guest(guest_probe_script(canary_ip, canary_port, check_dns=check_dns,
                                           more=canaries[1:], peers=peers))
        # A completed handshake sits in the backlog whether or not it was
        # accepted, so the listeners are the authority on whether a canary was
        # reached.
        reached, _, _ = select.select(listeners, [], [], 0.5)

    match = _RESULT.search(out)
    rc = int(match.group(1)) if match else None
    if rc == _LEAK or reached:
        hit = ", ".join(f"{ip}:{port}" for (ip, port), ln in zip(canaries, listeners)
                        if ln in reached) or f"{canary_ip}:{canary_port}"
        die(f"ISOLATION FAILURE: {what} reached the host canary "
            f"{hit}. The egress boundary is not in "
            f"force — refusing to run the agent (fail-closed). {remedy}")
    if rc == _PUBLIC_LEAK:
        die(f"ISOLATION FAILURE: {what} connected to {_PUBLIC_IP}:443 without "
            f"the gateway. The egress boundary is not in force — refusing to "
            f"run the agent (fail-closed). {remedy}")
    if rc == _PEER_LEAK:
        peer = _PEER.search(out)
        die(f"ISOLATION FAILURE: {what} reached the sibling agent at "
            f"{peer.group(1) if peer else 'an unknown address'}. Agents must not "
            f"reach each other — refusing to run the agent (fail-closed). {remedy}")
    if rc == _DNS_LEAK:
        die(f"ISOLATION FAILURE: {what} resolved {_DNS_NAME} without the "
            f"gateway, so DNS is a channel out of the box. Refusing to run the "
            f"agent (fail-closed). {remedy}")
    if rc == _NO_TOOL:
        tools = "python3/bash" if peers else "python3/bash/nc"
        die(f"isolation probe inconclusive: {what} has no {tools} "
            f"to run the connectivity test. Refusing to continue (fail-closed).")
    if rc != _BLOCKED:
        die(f"isolation probe did not run in {what} "
            f"(result {rc if rc is not None else 'missing'}). Refusing to "
            f"continue (fail-closed).")
    siblings = f" or sibling agents {', '.join(peers)}" if peers else ""
    info(f"isolation verified: {what} cannot reach host "
         f"{', '.join(ip for ip, _ in canaries)}{siblings}")
    if key:
        _remember(key)
