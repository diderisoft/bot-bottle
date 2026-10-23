"""Stable gateway name for macOS agents, via each bottle's `/etc/hosts`.

The shared gateway's address is assigned by vmnet's DHCP and changes whenever
the gateway container is recreated. Every agent-facing URL (egress proxy,
git-http, supervise) names the gateway (`GATEWAY_HOSTNAME`) instead, resolved
through the bottle's own `/etc/hosts`: unlike the agent's `environ`, that file
can be rewritten in a running container, so the reattach flow
re-points live bottles when the gateway moves. Apple Container 1.0 has no
container-name DNS and no `--add-host`, so the entry is written by exec.

Writing it needs root and the agent runs as `node`, so the agent cannot
repoint its own gateway; the host can. The rewrite runs an absolute `/bin/sh`
with a pinned PATH and stages under root-only `/root`, never `/tmp`, where the
agent could plant a symlink.
"""

from __future__ import annotations

import ipaddress

from ..util import ROOT_EXEC_PATH
from . import util as container_mod

# The name every agent-facing gateway URL uses. Must not collide with a real
# DNS name the agent might resolve; it is bottle-local by construction.
GATEWAY_HOSTNAME = "bot-bottle-gateway"

# Marker so the rewrite is idempotent and only ever touches our own line —
# the rest of /etc/hosts (localhost, the container's own name) is preserved.
_MARKER = "# bot-bottle gateway"
_STAGED_HOSTS = "/root/.bot-bottle-hosts"


def gateway_host_argv(gateway_ip: str) -> list[str]:
    """The root command (run via `container exec --user root`) that points
    `GATEWAY_HOSTNAME` at `gateway_ip`. Rewrites in place via `cat` rather than
    `mv`, so `/etc/hosts` keeps its inode, ownership and mode. Raises
    ValueError if `gateway_ip` isn't an IP address."""
    ip = str(ipaddress.ip_address(gateway_ip))
    script = (
        f"export PATH={ROOT_EXEC_PATH}; set -e; "
        f"grep -v '{_MARKER}' /etc/hosts > {_STAGED_HOSTS} || true; "
        f"printf '%s %s %s\\n' '{ip}' '{GATEWAY_HOSTNAME}' '{_MARKER}' >> {_STAGED_HOSTS}; "
        f"cat {_STAGED_HOSTS} > /etc/hosts; "
        f"rm -f {_STAGED_HOSTS}"
    )
    return ["/bin/sh", "-c", script]


def set_gateway_host(container_name: str, gateway_ip: str) -> None:
    """Point `GATEWAY_HOSTNAME` at `gateway_ip` inside one running container.
    Must run before the agent is exec'd, so the entry exists for its first
    connection. Idempotent."""
    container_mod.exec_container_as_root(container_name, gateway_host_argv(gateway_ip))


__all__ = ["GATEWAY_HOSTNAME", "gateway_host_argv", "set_gateway_host"]
