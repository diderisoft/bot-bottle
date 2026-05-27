"""Smolfile (TOML) renderer for the smolmachines backend (PRD 0023).

The Smolfile shape comes from `smolvm machine create --smolfile`
in smolvm 0.8.0. The renderer emits only the runtime overrides
that vary per bottle:

  - `env = ["K=V", ...]` — agent's HTTPS_PROXY / NO_PROXY /
    NODE_EXTRA_CA_CERTS, all using IP literals pointing at the
    per-bottle sidecar bundle's pinned docker IP.

  - `[network] allow_cidrs = ["<bundle-ip>/32"]` — TSI's single-IP
    allowlist. With this and no other `allow_*` or
    `outbound-localhost-only`, the agent can dial exactly one IP:
    the bundle. Host loopback, LAN, and the public internet
    directly are all refused at the VMM layer.

What the renderer does NOT emit:

  - `image` / `entrypoint` / `cmd` — those come from the
    `.smolmachine` artifact (produced by `smolvm pack create
    --image claude-bottle:latest`) and don't vary across bottles
    of the same agent image.

  - `cpus` / `memory` — left at smolvm defaults until the
    operator surfaces a need to override per bottle (the manifest
    has no such field today).

Pure function; disk writes happen via `smolfile_write`."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def smolfile_build(
    *,
    env: Mapping[str, str],
    bundle_ip: str,
) -> dict[str, Any]:
    """Build the Smolfile config dict. `env` is `{NAME: VALUE}` for
    the guest's process env (IP-literal HTTPS_PROXY etc.).
    `bundle_ip` is the pinned docker IP of the per-bottle sidecar
    bundle; it lands in `[network] allow_cidrs` as a /32."""
    return {
        "env": [f"{k}={v}" for k, v in sorted(env.items())],
        "network": {
            "allow_cidrs": [f"{bundle_ip}/32"],
        },
    }


def smolfile_render(cfg: dict[str, Any]) -> str:
    """Render the Smolfile dict as TOML. Schema is narrow (string
    list + one table with a string list) so we render by hand and
    stay stdlib-only."""
    lines: list[str] = []
    lines.append(f'env = {_toml_array(cfg["env"])}')
    lines.append("")
    lines.append("[network]")
    lines.append(f'allow_cidrs = {_toml_array(cfg["network"]["allow_cidrs"])}')
    return "\n".join(lines) + "\n"


def smolfile_write(cfg: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(smolfile_render(cfg))
    path.chmod(0o600)
    return path


def _toml_str(value: Any) -> str:
    """TOML basic string: double-quoted with backslash + double-quote
    escapes."""
    s = str(value)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _toml_array(values: list[Any]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"
