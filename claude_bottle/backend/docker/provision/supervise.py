"""Supervise sidecar provisioning inside a running Docker bottle
(PRD 0013).

Registers the per-bottle supervise sidecar as an HTTP MCP server in
the agent's claude-code config so the agent discovers the three
stuck-recovery MCP tools (cred-proxy-block, pipelock-block,
capability-block) at startup.

Uses `claude mcp add` rather than writing JSON directly. claude-code
owns the on-disk config format (`~/.claude.json` `mcpServers` shape,
field names, scope semantics) and changes it between versions; the
official command handles whatever the installed version expects.

No-op when bottle.supervise is False — bottles that haven't opted
into the supervise sidecar shouldn't get an MCP entry pointing at a
sidecar that isn't running.
"""

from __future__ import annotations

import subprocess

from ....log import info, warn
from ....supervise import SUPERVISE_HOSTNAME, SUPERVISE_PORT
from ..bottle_plan import DockerBottlePlan


_SUPERVISE_MCP_NAME = "supervise"


def supervise_mcp_url() -> str:
    return f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}/"


def provision_supervise(plan: DockerBottlePlan, target: str) -> None:
    """Run `claude mcp add` inside the agent container to register
    the supervise sidecar in claude-code's user config. No-op when
    bottle.supervise is False.

    Failure is logged but not fatal: the bottle still works (you
    just can't call supervise tools from the agent until the entry
    is added manually). The operator sees the warning at launch."""
    if plan.supervise_plan is None:
        return
    url = supervise_mcp_url()
    argv = [
        "docker", "exec", "-u", "node", target,
        "claude", "mcp", "add",
        "--scope", "user",
        "--transport", "http",
        _SUPERVISE_MCP_NAME,
        url,
    ]
    info(f"registering supervise MCP server in agent claude config → {url}")
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        warn(
            f"`claude mcp add supervise` failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
            f"register manually with: "
            f"claude mcp add --scope user --transport http supervise {url}"
        )


__all__ = ["provision_supervise", "supervise_mcp_url"]
