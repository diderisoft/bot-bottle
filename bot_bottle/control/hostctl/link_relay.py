"""Which iroh relay the console link uses (the console's `relay/`).

A host behind NAT reaches its console only through a relay, and iroh's public
relays are rate-limited. A console that runs its own says so at
`GET <console>/api/v1/iroh/relay`; the link then makes that relay its home
relay too, so traffic both ways goes through it. A console that doesn't
(`null`), or one this can't ask, leaves iroh's public relays, as before. The
relay only lets in the console and the hosts paired with it.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from ...lib import log

RELAY_PATH = "/api/v1/iroh/relay"
TIMEOUT_SECONDS = 5.0


def console_relay(console_url: str, timeout: float = TIMEOUT_SECONDS) -> str:
    """The relay URL the console at `console_url` asks its hosts to use, or ""
    for iroh's public relays (also when the console can't be asked)."""
    if urlsplit(console_url).scheme not in ("https", "http"):
        return ""
    try:
        with urllib.request.urlopen(console_url.rstrip("/") + RELAY_PATH,  # nosec B310: http(s) only
                                    timeout=timeout) as resp:
            relay = json.load(resp).get("relay_url") or ""
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
        log.debug(f"remote: can't ask the console for its relay: {e}")
        return ""
    if not isinstance(relay, str) or urlsplit(relay).scheme not in ("https", "http"):
        return ""
    return relay


def endpoint_options(iroh: Any, relay: str, secret_key: bytes) -> Any:
    """The link's endpoint options: `relay` as its only relay, or iroh's."""
    if relay:
        log.info(f"remote: using the console's relay {relay}")
        return iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret_key, alpns=[],
                                    relay_mode=iroh.RelayMode.custom_from_urls([relay]))
    return iroh.EndpointOptions(preset=iroh.preset_n0(), secret_key=secret_key, alpns=[])


async def bind(iroh: Any, console_url: str, secret_key: bytes) -> Any:
    """The link's iroh endpoint, on the console's relay when it names one."""
    relay = await asyncio.to_thread(console_relay, console_url)
    return await iroh.Endpoint.bind(endpoint_options(iroh, relay, secret_key))


__all__ = ["RELAY_PATH", "bind", "console_relay", "endpoint_options"]
