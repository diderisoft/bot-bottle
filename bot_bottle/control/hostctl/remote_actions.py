"""Console requests about this host's credentials, answered over the link:
refreshing a running agent's forwarded host login, and connecting a forge
(PRD 0105). Each returns the reply to send."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...lib import log


async def refresh_credentials(msg: Mapping[str, Any], running: Mapping[str, str],
                              bottles: Any) -> dict[str, Any]:
    """Re-read a running agent's forwarded host login now and hand it to the
    gateway (`credential_refresh`)."""
    agent_id = str(msg.get("agent_id", ""))
    bottle = running.get(agent_id)
    refresh = getattr(bottles, "refresh_credentials", None)  # BottleService's
    result: dict[str, object] = (
        {"error": "that agent isn't running on this host"} if not bottle
        else {"error": "this host controller can't refresh credentials"} if refresh is None
        else await asyncio.to_thread(refresh, bottle))
    return {"type": "credentials_refreshed", "request_id": msg.get("request_id"),
            "agent_id": agent_id, **result}


def _text(msg: Mapping[str, Any], key: str, default: str = "") -> str:
    value = msg.get(key, default)
    return value if isinstance(value, str) else default


async def forge_connect(msg: Mapping[str, Any]) -> dict[str, Any]:
    """Connect a forge from the console (`forge_connect.connect`), into a host
    bottle. The token arrives in the message and goes only to the forge and
    the host secrets file: never into the reply or a log."""
    # pylint: disable=import-outside-toplevel
    from ...forge_connect import ConnectError, connect
    from ...paths import bot_bottle_root
    reply: dict[str, Any] = {"type": "forge_connected", "request_id": msg.get("request_id")}
    bottle = _text(msg, "bottle", "gitea") or "gitea"
    if bottle.startswith("./"):
        reply["error"] = "the console connects forges into host bottles, not a project's"
        return reply
    try:
        done = await asyncio.to_thread(
            connect, _text(msg, "url"), _text(msg, "token"), bottle=bottle,
            name=_text(msg, "name"), ssh=_text(msg, "ssh"), cwd=Path.home(),
            host_bottles=bot_bottle_root() / "bottles")
    except ConnectError as e:
        reply["error"] = str(e)
        return reply
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: connecting a forge failed: {type(e).__name__}")
        reply["error"] = "connecting failed; see the host controller's log"
        return reply
    log.info(f"remote: connected {done.key} as {done.account} into bottle {done.bottle}")
    reply.update(key=done.key, bottle=done.bottle, account=done.account,
                 admin_repos=done.admin_repos, repos=done.repos, ssh=done.ssh,
                 host_key=done.host_key, warnings=list(done.warnings))
    return reply


__all__ = ["forge_connect", "refresh_credentials"]
