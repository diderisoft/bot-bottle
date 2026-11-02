"""handle_get_environment MCP tool: return orchestrator version info."""

from __future__ import annotations

import os


def handle_get_environment(_params: dict[str, object]) -> dict[str, object]:
    info = {"version": os.environ.get("BOT_BOTTLE_VERSION", ""),
            "commit": os.environ.get("BOT_BOTTLE_COMMIT", ""),
            "protocol": os.environ.get("BOT_BOTTLE_PROTOCOL", "")}
    text = "\n".join(f"{k}: {v}" for k, v in info.items() if v) or "version unknown"
    return {"content": [{"type": "text", "text": text}], "isError": False}
