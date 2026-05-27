"""gvproxy YAML config renderer (PRD 0023).

The gvproxy config defines:

  - The per-bottle subnet + gateway IP — derived from the slug so
    parallel bottles don't collide on 192.168.X.0/24.
  - A DNS rule that resolves only `proxy.internal` to the gateway.
    Every other hostname returns NXDOMAIN. This is the load-bearing
    rule for PRD 0022's DNS-exfil attack: the guest can't `dig`
    arbitrary names.
  - `port_forwards` — one entry per sidecar daemon the bottle uses.
    Only what's listed here is reachable from the guest. The
    host-side sidecar bundle listens on the resolved `host_port`s;
    gvproxy port-forwards `gateway_port` (what the guest dials) →
    host `host_port`.

This is the file the PRD 0023 design names as the network
primitive. TSI is explicitly NOT used — see PRD 0023 "Why gvproxy,
not TSI".

The renderer is pure; disk writes happen in prepare.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PortForward:
    """One gvproxy port forward. The guest dials `gateway_port`; the
    host receives the connection at `127.0.0.1:host_port`."""
    gateway_port: int
    host_port: int


def gvproxy_config_build(
    *,
    subnet: str,
    gateway: str,
    port_forwards: tuple[PortForward, ...],
) -> dict[str, Any]:
    """Build the gvproxy YAML config dict. The shape matches the
    recipe in `agent-vm-isolation.md` § "Full Setup": top-level
    `subnet`, `gateway`, `dns:` with the `proxy.internal` carve-out,
    and `port_forwards`."""
    return {
        "subnet": subnet,
        "gateway": gateway,
        "dns": [
            {
                "zone": ".",
                "records": [
                    {"name": "proxy.internal", "ip": gateway},
                ],
            },
        ],
        "port_forwards": [
            {
                "gateway_port": p.gateway_port,
                "host": "127.0.0.1",
                "host_port": p.host_port,
            }
            for p in port_forwards
        ],
    }


def gvproxy_config_render(cfg: dict[str, Any]) -> str:
    """Render the gvproxy config as a small YAML subset (the same
    shape `pipelock_render_yaml` uses). Stdlib has no YAML writer
    and the config shape is narrow — strings + ints + lists of dicts
    — so rendering by hand is straightforward and keeps the project
    stdlib-only."""
    lines: list[str] = []
    lines.append(f'subnet: "{cfg["subnet"]}"')
    lines.append(f'gateway: "{cfg["gateway"]}"')
    lines.append("dns:")
    for zone in cfg["dns"]:
        lines.append(f'  - zone: "{zone["zone"]}"')
        lines.append("    records:")
        for record in zone["records"]:
            lines.append(f'      - name: "{record["name"]}"')
            lines.append(f'        ip: "{record["ip"]}"')
    if cfg["port_forwards"]:
        lines.append("port_forwards:")
        for pf in cfg["port_forwards"]:
            lines.append(f'  - gateway_port: {pf["gateway_port"]}')
            lines.append(f'    host: "{pf["host"]}"')
            lines.append(f'    host_port: {pf["host_port"]}')
    else:
        lines.append("port_forwards: []")
    return "\n".join(lines) + "\n"


def gvproxy_config_write(cfg: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gvproxy_config_render(cfg))
    path.chmod(0o600)
    return path
