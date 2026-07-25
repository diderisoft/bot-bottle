"""Stable gateway name for macOS agents, via each bottle's `/etc/hosts`.

The shared gateway's address is assigned by vmnet's DHCP and changes whenever
the gateway container is recreated — a source-hash bump, an image upgrade, a
crash. Every agent-facing URL (egress proxy, git-http, supervise) embeds that
address, and the proxy URL reaches the agent as **process environment** at
`container exec` time. A running process's `environ` cannot be rewritten from
outside, so a moved gateway used to strand every running bottle permanently:
not degraded, unreachable, until the bottle was relaunched and its session
thrown away.

So the agent never learns the address. It is given a stable *name*
(`GATEWAY_HOSTNAME`) in every URL, resolved through its own `/etc/hosts`.
Unlike `environ`, that is a file — it can be rewritten inside a container that
is already running, so a gateway that comes back at a new address is picked up
by live bottles instead of orphaning them.

Apple Container 1.0 offers no container-name DNS on a user network (the only
nameserver an agent sees is vmnet's, which does not know container names) and
`container run` has no `--add-host`, so the entry is written by exec after the
container starts.

Writing it needs root, and the agent runs as `node`: the agent therefore
cannot repoint its own gateway name, while the host (which drives `container
exec --user root`) can. That asymmetry is deliberate — keep it.
"""

from __future__ import annotations

from ...log import warn
from . import util as container_mod
from .enumerate import CONTAINER_NAME_PREFIX, enumerate_active

# The name every agent-facing gateway URL uses. Must not collide with a real
# DNS name the agent might resolve; it is bottle-local by construction.
GATEWAY_HOSTNAME = "bot-bottle-gateway"

# Marker so the rewrite is idempotent and only ever touches our own line —
# the rest of /etc/hosts (localhost, the container's own name) is preserved.
_MARKER = "# bot-bottle gateway"


def _rewrite_script(gateway_ip: str) -> str:
    """A shell one-liner that replaces our managed line in `/etc/hosts`.

    Rewrites in place via a temp file + `cat` rather than `mv`, so the file
    keeps its original inode, ownership, and mode — a bind-mounted or
    pre-created `/etc/hosts` must not be replaced by a root-owned 0644 copy
    that the runtime then refuses to update.
    """
    return (
        "set -e; "
        f"grep -v '{_MARKER}' /etc/hosts > /tmp/.bb-hosts || true; "
        f"printf '%s %s %s\\n' '{gateway_ip}' '{GATEWAY_HOSTNAME}' "
        f"'{_MARKER}' >> /tmp/.bb-hosts; "
        "cat /tmp/.bb-hosts > /etc/hosts; "
        "rm -f /tmp/.bb-hosts"
    )


def set_gateway_host(container_name: str, gateway_ip: str) -> None:
    """Point `GATEWAY_HOSTNAME` at `gateway_ip` inside one running container.

    Must run before the agent is exec'd: the agent's proxy URL names the
    gateway, so the entry has to exist for its first connection. Idempotent —
    re-running with the same address is a no-op in effect.
    """
    container_mod.exec_container_as_root(
        container_name, ["sh", "-c", _rewrite_script(gateway_ip)],
    )


def refresh_gateway_host(gateway_ip: str) -> list[str]:
    """Re-point every running bottle at the current gateway address.

    Called once the shared gateway is known to be up, so a bottle stranded by
    an earlier gateway restart re-attaches instead of needing a relaunch.
    Returns the containers updated.

    Best-effort per bottle: one container that refuses the write (already
    exiting, say) must not stop the others from being repaired, and must not
    fail the launch that triggered the sweep.
    """
    updated: list[str] = []
    for agent in enumerate_active():
        name = f"{CONTAINER_NAME_PREFIX}{agent.slug}"
        try:
            set_gateway_host(name, gateway_ip)
            updated.append(name)
        # One bad bottle must not stop the sweep, so this is deliberately broad.
        except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            warn(f"could not re-point {name} at the gateway: {e}")
    return updated


__all__ = ["GATEWAY_HOSTNAME", "set_gateway_host", "refresh_gateway_host"]
