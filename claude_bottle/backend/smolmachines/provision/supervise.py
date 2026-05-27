"""Supervise sidecar provisioning inside a running smolmachines
bottle (PRD 0023 chunk 4d; PRD 0013 supervise plane).

Registers the per-bottle supervise sidecar as an HTTP MCP server
in the agent's claude-code config so the agent discovers the
stuck-recovery MCP tools (pipelock-block, capability-block) at
startup.

Mirrors `backend.docker.provision.supervise` — same `claude mcp
add` call, just dispatched via `smolvm machine exec` instead of
`docker exec`, and against `<bundle_ip>:<port>` instead of the
short `supervise` alias (no DNS in the TSI-allowlisted guest)."""

from __future__ import annotations

from ....log import info, warn
from ....supervise import SUPERVISE_PORT
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


_SUPERVISE_MCP_NAME = "supervise"


def supervise_mcp_url(bundle_ip: str) -> str:
    return f"http://{bundle_ip}:{SUPERVISE_PORT}/"


def provision_supervise(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Run `claude mcp add` inside the guest to register the
    supervise sidecar in claude-code's user config. No-op when
    bottle.supervise is False.

    Failure is logged but not fatal: the bottle still works (you
    just can't call supervise tools from the agent until the entry
    is added manually). The operator sees the warning at launch."""
    if plan.supervise_plan is None:
        return
    url = supervise_mcp_url(plan.bundle_ip)
    info(f"registering supervise MCP server in agent claude config → {url}")
    r = _smolvm.machine_exec(
        target,
        [
            "claude", "mcp", "add",
            "--scope", "user",
            "--transport", "http",
            _SUPERVISE_MCP_NAME,
            url,
        ],
    )
    if r.returncode != 0:
        warn(
            f"`claude mcp add supervise` failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
            f"register manually with: "
            f"claude mcp add --scope user --transport http supervise {url}"
        )


__all__ = ["provision_supervise", "supervise_mcp_url"]
