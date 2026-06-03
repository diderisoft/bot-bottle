"""Supervise sidecar provisioning inside a running smolmachines
bottle (PRD 0023 chunk 4d; PRD 0013 supervise plane).

Registers the per-bottle supervise sidecar as an HTTP MCP server
in the agent's claude-code config so the agent discovers the
stuck-recovery MCP tools (pipelock-block, capability-block) at
startup.

Mirrors `backend.docker.provision.supervise` — same `claude mcp
add` call, just dispatched via bottle.exec instead of
`docker exec`, and against `<bundle_ip>:<port>` instead of the
short `supervise` alias (no DNS in the TSI-allowlisted guest)."""

from __future__ import annotations

from ....log import info, warn
from ... import Bottle
from ..bottle_plan import SmolmachinesBottlePlan


_SUPERVISE_MCP_NAME = "supervise"


def provision_supervise(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """Run `claude mcp add` inside the guest to register the
    supervise sidecar in claude-code's user config. No-op when
    bottle.supervise is False.

    The URL is the agent-side endpoint launch.py populated after
    bundle bringup — `http://127.0.0.1:<host port>/` rather than
    the bundle's docker bridge IP, because that bridge isn't
    reachable from the smolvm guest on macOS.

    Failure is logged but not fatal: the bottle still works (you
    just can't call supervise tools from the agent until the entry
    is added manually). The operator sees the warning at launch."""
    if plan.supervise_plan is None:
        return
    url = plan.agent_supervise_url
    info(f"registering supervise MCP server in agent claude config → {url}")
    # `claude mcp add --scope user` writes to ~/.claude.json. Run
    # as node so the config lands in /home/node/.claude.json.
    # SmolmachinesBottle.exec sets HOME and USER automatically
    # for the requested user.
    r = bottle.exec(
        f"claude mcp add --scope user --transport http {_SUPERVISE_MCP_NAME} {url}",
        user="node",
    )
    if r.returncode != 0:
        warn(
            f"`claude mcp add supervise` failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
            f"register manually with: "
            f"claude mcp add --scope user --transport http supervise {url}"
        )


__all__ = ["provision_supervise"]
