"""The firewall in each macOS agent's VM: the gateway and nothing else, in
either direction.

Agents share one vmnet network, and frames between them are switched on the
bridge without reaching the IP layer, so the host's pf can't filter them. The
agent's own kernel can. The rules let the agent reach the gateway's address
and drop everything else it sends, and drop every connection that isn't a
reply. Nothing connects into an agent on this backend (control is `container
exec`) and its address is static, so no inbound rule is needed.

Root's own traffic is exempt: the agent isn't root, and bot-bottle's
launch-time probe runs as root to test pf on its own. An agent that can get
root can remove the rules; the launch warns about it
(`privilege_check_script`).

The rules are kernel state, so they're loaded on every start of the guest and
again when the gateway moves, in the same root exec that points
`GATEWAY_HOSTNAME` at it.
"""

from __future__ import annotations

import ipaddress
import re

from ..util import ROOT_EXEC_PATH
from . import util as container_mod
from .gateway_hosts import gateway_host_script

TABLE = "bot_bottle"

_STATUS = re.compile(r"^bb-firewall=(\S+)$", re.MULTILINE)
_PRIVILEGE = re.compile(r"^bb-privilege=(.+)$", re.MULTILINE)


def ruleset(gateway_ip: str) -> str:
    """The nft script. Declaring then deleting the table first makes loading
    it replace any earlier one in one transaction. Raises ValueError for a
    non-IP."""
    ip = str(ipaddress.ip_address(gateway_ip))
    family = "ip6" if ":" in ip else "ip"
    return (
        f"table inet {TABLE}\n"
        f"delete table inet {TABLE}\n"
        f"table inet {TABLE} {{\n"
        "  chain output {\n"
        "    type filter hook output priority 0; policy drop;\n"
        '    oifname "lo" accept\n'
        "    meta skuid 0 accept\n"
        "    ct state established,related accept\n"
        f"    {family} daddr {ip} accept\n"
        # Fail at once rather than time out. Not a refusal, which the
        # isolation probe would take for a sibling answering.
        "    reject with icmpx admin-prohibited\n"
        "  }\n"
        "  chain input {\n"
        "    type filter hook input priority 0; policy drop;\n"
        '    iifname "lo" accept\n'
        "    ct state established,related accept\n"
        "  }\n"
        "}\n"
    )


def firewall_script(gateway_ip: str) -> str:
    """Root shell that loads `ruleset` and prints `bb-firewall=ok`,
    `bb-firewall=no-nft` when the image has no `nft`, or `bb-firewall=failed`
    with nft's error when it refuses it. It exits zero either way so the
    caller gets the status, not a retry."""
    return (
        f"export PATH={ROOT_EXEC_PATH}; "
        "if ! command -v nft >/dev/null 2>&1; then echo bb-firewall=no-nft; exit 0; fi; "
        f"if err=$(printf '%s' '{ruleset(gateway_ip)}' | nft -f - 2>&1); "
        'then echo bb-firewall=ok; else echo bb-firewall=failed; echo "$err"; fi'
    )


def point_at_gateway_argv(gateway_ip: str) -> list[str]:
    """Root command that points `GATEWAY_HOSTNAME` at `gateway_ip` and loads
    the firewall allowing only it."""
    return ["/bin/sh", "-c",
            f"{gateway_host_script(gateway_ip)}; {firewall_script(gateway_ip)}"]


def firewall_loaded(output: str) -> bool:
    """Whether `point_at_gateway_argv`'s output says the rules are in force
    (False only for an image without `nft`). Raises ValueError when they
    failed to load or it reported nothing."""
    match = _STATUS.search(output)
    status = match.group(1) if match else ""
    if status not in ("ok", "no-nft"):
        detail = output[match.end():].strip() if status == "failed" and match else ""
        raise ValueError("the agent's firewall didn't load"
                         + (f": {detail}" if detail else ""))
    return status == "ok"


def point_at_gateway(container: str, gateway_ip: str) -> bool:
    """Point `container` at the gateway and load its firewall, as root.
    False when the image has no `nft`. Raises ValueError when the rules
    don't load."""
    return firewall_loaded(
        container_mod.exec_container_as_root(container, point_at_gateway_argv(gateway_ip)))


# Run as the agent's user. Each way it could get root prints a line.
_PRIVILEGE_CHECK = r"""
[ "$(id -u)" = 0 ] && echo "bb-privilege=runs as root"
command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1 \
  && echo "bb-privilege=passwordless sudo"
command -v doas >/dev/null 2>&1 && doas -n true >/dev/null 2>&1 \
  && echo "bb-privilege=passwordless doas"
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY'
import os
seen = set()
for d in os.environ.get("PATH", "").split(":"):
    try:
        names = os.listdir(d)
    except OSError:
        continue
    for name in names:
        path = os.path.realpath(os.path.join(d, name))
        if path in seen:
            continue
        seen.add(path)
        try:
            os.getxattr(path, "security.capability")
        except OSError:
            continue
        print(f"bb-privilege=file capabilities on {path}")
PY
fi
exit 0
"""


def privilege_check_script() -> str:
    """Shell for the agent's user that prints a `bb-privilege=<how>` line for
    each way it can get root: running as root, passwordless sudo or doas, or a
    binary on PATH with file capabilities. It can't prove there is none."""
    return _PRIVILEGE_CHECK


def privileges(output: str) -> list[str]:
    """The ways `privilege_check_script` found, in order."""
    return [m.group(1).strip() for m in _PRIVILEGE.finditer(output)]


__all__ = [
    "TABLE", "firewall_loaded", "firewall_script", "point_at_gateway",
    "point_at_gateway_argv", "privilege_check_script", "privileges", "ruleset",
]
