"""The host's side of the console link's protocol (ADR 0024): its `hello`, the
`unsupported` reply, and what it makes of the console's `hello`. The contract
itself is `contract/console_link.py`."""

from __future__ import annotations

from typing import Any

from ...contract.console_link import LINK_PROTOCOL, UNSUPPORTED
from ...lib import log


def hello() -> dict[str, Any]:
    """The host's `hello`: the link protocol it speaks and its version."""
    # pylint: disable-next=import-outside-toplevel
    from ...engine import install_version
    return {"type": "hello", "protocol": LINK_PROTOCOL, "version": install_version()[0]}


def unsupported(msg: dict[str, Any]) -> dict[str, Any]:
    """The reply to a message this host doesn't speak, so the console's request
    fails at once rather than waiting out its timeout. Logged."""
    kind = str(msg.get("type", ""))[:64]
    log.warn(f"remote: the console sent {kind!r}, which this host doesn't speak "
             f"(link protocol {LINK_PROTOCOL})")
    return {"type": UNSUPPORTED, "request_id": msg.get("request_id"), "message_type": kind,
            "error": f"this host doesn't speak {kind!r}; upgrade bot-bottle"}


def console_hello(msg: dict[str, Any]) -> None:
    """Log a console that doesn't speak this host's protocol; the console
    gates the link. A console that says none predates the protocol."""
    spoken = msg.get("protocols")
    if not (isinstance(spoken, list) and len(spoken) == 2
            and all(isinstance(p, int) and not isinstance(p, bool) for p in spoken)):
        return
    low, high = spoken
    if low <= LINK_PROTOCOL <= high:
        return
    log.warn(f"remote: the console speaks link protocol {low}-{high}, this host "
             f"{LINK_PROTOCOL}; " + ("upgrade bot-bottle" if LINK_PROTOCOL < low
                                     else "the console needs updating"))


__all__ = ["console_hello", "hello", "unsupported"]
