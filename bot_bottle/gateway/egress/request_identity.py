"""Who a request is from and where it says it's going: the identity token in
its proxy credentials, and whether the host it names is the one it's
addressed to."""

from __future__ import annotations

import base64
import binascii
import typing

from bot_bottle.gateway.egress.matching import _strip_port


def norm_host(host: str) -> str:
    """`host` as policy keys it: no port, no trailing dot, lowercase."""
    return _strip_port(host.strip()).rstrip(".").lower()


def host_mismatch(flow: typing.Any) -> str:
    """Why the host a request names differs from the one mitmproxy connects
    to, or "". Policy keys on that host, so a Host header, `:authority` or SNI
    naming another one would pass as it."""
    target = norm_host(flow.request.host)
    named = norm_host(flow.request.pretty_host)
    if named != target:
        return (f"egress: request names host {named!r} but is addressed to "
                f"{target!r}")
    conn = getattr(flow, "client_conn", None)
    sni = getattr(conn, "sni", None) if conn is not None else None
    if sni and norm_host(sni) != target:
        return (f"egress: TLS SNI {norm_host(sni)!r} doesn't match the "
                f"addressed host {target!r}")
    return ""


def token_from_proxy_auth(header: str) -> str:
    """The identity token (the password) from `Proxy-Authorization: Basic
    base64(<bottle_id>:<token>)`; "" if malformed, which `/resolve` then
    denies."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""
    _, _, password = decoded.partition(":")
    return password
