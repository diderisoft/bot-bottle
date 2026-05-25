"""Supervise sidecar provisioning inside a running Docker bottle
(PRD 0013).

Writes ~/.claude/settings.json with an `mcpServers.supervise` entry
pointing at the per-bottle supervise sidecar so the in-bottle Claude
Code discovers the three stuck-recovery MCP tools (cred-proxy-block,
pipelock-block, capability-block) at startup.

No-op when bottle.supervise is False — bottles that haven't opted
into the supervise sidecar shouldn't get an MCP entry pointing at a
sidecar that isn't running.
"""

from __future__ import annotations

import json
import os
import subprocess

from ....log import info
from ....supervise import SUPERVISE_HOSTNAME, SUPERVISE_PORT
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan


_AGENT_HOME_DEFAULT = "/home/node"
_SETTINGS_REL_PATH = ".claude/settings.json"
_SUPERVISE_MCP_NAME = "supervise"


def render_settings() -> str:
    """The settings.json content the agent reads on startup. Stable
    shape — only the URL is parameterized in case CLAUDE_BOTTLE_*
    env overrides change the supervise hostname/port someday."""
    cfg = {
        "mcpServers": {
            _SUPERVISE_MCP_NAME: {
                "type": "http",
                "url": f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}/",
            },
        },
    }
    return json.dumps(cfg, indent=2) + "\n"


def provision_supervise(plan: DockerBottlePlan, target: str) -> None:
    """Drop ~/.claude/settings.json into the running agent container
    when bottle.supervise is True. No-op otherwise."""
    if plan.supervise_plan is None:
        return

    container_home = os.environ.get(
        "CLAUDE_BOTTLE_CONTAINER_HOME", _AGENT_HOME_DEFAULT,
    )
    settings_in_container = f"{container_home}/{_SETTINGS_REL_PATH}"
    settings_dir_in_container = settings_in_container.rsplit("/", 1)[0]

    host_path = plan.stage_dir / "agent_claude_settings.json"
    host_path.write_text(render_settings())
    host_path.chmod(0o644)

    info(f"writing {settings_in_container} (supervise MCP server entry)")
    # The Dockerfile creates ~/.claude.json at the top of HOME but
    # not the ~/.claude/ subdir, so make sure it exists before cp.
    docker_mod.docker_exec_root(target, ["mkdir", "-p", settings_dir_in_container])
    subprocess.run(
        ["docker", "cp", str(host_path), f"{target}:{settings_in_container}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(target, ["chown", "-R", "node:node", settings_dir_in_container])
    docker_mod.docker_exec_root(target, ["chmod", "644", settings_in_container])


__all__ = ["provision_supervise", "render_settings"]
