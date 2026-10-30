"""Stable gateway name for macOS agents through each bottle's `/etc/hosts`.

The gateway's DHCP address changes when it's recreated, so agent URLs use
`GATEWAY_HOSTNAME`, which reattach can re-point in a running container (unlike
`environ`). Apple has no container DNS or `--add-host`. The write needs root,
so the agent can't re-point it; it runs `/bin/sh` with a pinned PATH and stages
under `/root`.
"""

from __future__ import annotations

import ipaddress

from ..util import ROOT_EXEC_PATH

# Bottle-local name every agent-facing gateway URL uses.
GATEWAY_HOSTNAME = "bot-bottle-gateway"

# Marks our line so the rewrite is idempotent and leaves the rest alone.
_MARKER = "# bot-bottle gateway"
_STAGED_HOSTS = "/root/.bot-bottle-hosts"


def gateway_host_script(gateway_ip: str) -> str:
    """Root shell pointing `GATEWAY_HOSTNAME` at `gateway_ip`. Rewrites with
    `cat` so `/etc/hosts` keeps its inode and mode. Raises ValueError for a
    non-IP."""
    ip = str(ipaddress.ip_address(gateway_ip))
    return (
        f"export PATH={ROOT_EXEC_PATH}; set -e; "
        f"grep -v '{_MARKER}' /etc/hosts > {_STAGED_HOSTS} || true; "
        f"printf '%s %s %s\\n' '{ip}' '{GATEWAY_HOSTNAME}' '{_MARKER}' >> {_STAGED_HOSTS}; "
        f"cat {_STAGED_HOSTS} > /etc/hosts; "
        f"rm -f {_STAGED_HOSTS}"
    )


def gateway_host_argv(gateway_ip: str) -> list[str]:
    """`gateway_host_script` as a root command."""
    return ["/bin/sh", "-c", gateway_host_script(gateway_ip)]


__all__ = ["GATEWAY_HOSTNAME", "gateway_host_argv", "gateway_host_script"]
