"""Smolfile (TOML) renderer for the smolmachines backend (PRD 0023).

The Smolfile pins the per-bottle microVM's command + env + virtio-net
device. Three fields drive what we emit:

  - `command` — the entrypoint claude-bottle runs inside the guest.
    Chunk 1 ships a placeholder (`sleep infinity`); chunk 4 wires
    the real `claude` entrypoint once provisioning is in place.

  - `env` — the agent's HTTP_PROXY / NO_PROXY / CA paths, pointing
    at `proxy.internal:<gateway-port>`. gvproxy resolves
    `proxy.internal` to the gateway IP and port-forwards to the
    host-side sidecar bundle.

  - `[[net]]` — a virtio-net device backed by gvproxy's unixgram
    socket via the VFKT handshake. This is the line that rejects
    libkrun's TSI mode: TSI's CIDR allowlist permits the entire
    127.0.0.0/8 of host loopback, which exposes every host-side
    service; gvproxy's explicit port-forward list is the only thing
    the guest can reach.

The renderer is a pure function. Disk writes happen in
`prepare.py` via `smolfile_write`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


# Default port assignments INSIDE the gvproxy network — what the
# guest dials. The agent's HTTPS_PROXY etc. resolve to
# `proxy.internal:<one of these>`. Host-side mapping is dynamic
# (chunk 3 allocates loopback ports per bottle).
GVPROXY_PIPELOCK_GATEWAY_PORT = 8888
GVPROXY_GIT_GATE_GATEWAY_PORT = 8889
GVPROXY_SUPERVISE_GATEWAY_PORT = 8890


def smolfile_build(
    *,
    slug: str,
    gvproxy_socket: Path,
    env: Mapping[str, str],
    command: tuple[str, ...] = ("sleep", "infinity"),
) -> dict[str, Any]:
    """Build the Smolfile config dict.

    `gvproxy_socket` is the unixgram socket gvproxy listens on; the
    guest's virtio-net device handshakes (VFKT magic) with it on
    start. `env` is `{NAME: VALUE}` for the guest's process env.
    `command` is the entrypoint argv inside the guest (placeholder
    until chunk 4 — see module docstring).

    Returns a TOML-shaped dict; render with `smolfile_render`."""
    return {
        "name": f"claude-bottle-{slug}",
        "command": list(command),
        "env": [f"{k}={v}" for k, v in sorted(env.items())],
        "net": [
            {
                "type": "virtio-net",
                "attachment": "unixgram",
                "socket": str(gvproxy_socket),
            },
        ],
    }


def smolfile_render(cfg: dict[str, Any]) -> str:
    """Render the Smolfile dict as TOML. Stdlib has `tomllib` for
    reading TOML but no writer; the smolmachines schema we emit is
    narrow enough (string scalars + string lists + one inline table
    per net device) to render by hand. Avoids a `tomli_w` runtime
    dep and keeps the project stdlib-only."""
    lines: list[str] = []
    lines.append(f'name = {_toml_str(cfg["name"])}')
    lines.append(f'command = {_toml_array(cfg["command"])}')
    lines.append(f'env = {_toml_array(cfg["env"])}')
    lines.append("")
    for net in cfg.get("net", ()):
        lines.append("[[net]]")
        for key, value in net.items():
            lines.append(f'{key} = {_toml_str(value)}')
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def smolfile_write(cfg: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(smolfile_render(cfg))
    path.chmod(0o600)
    return path


def _toml_str(value: Any) -> str:
    """TOML basic string: double-quoted with backslash + double-quote
    escapes. The smolmachines fields we emit (slugs, paths, env
    pairs) are ASCII-safe; the escape table covers what's reachable."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _toml_array(values: list[Any]) -> str:
    """TOML inline array. Uses `_toml_str` so quoting is consistent;
    the alternative would be `json.dumps(values)` which renders
    identical text for ASCII-only lists, but going through the same
    quoter is one less surprise on future inputs."""
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"
