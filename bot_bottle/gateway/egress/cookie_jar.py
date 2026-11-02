"""Cookies by origin for the egress gateway: a request's `Cookie` keeps only
the cookies that host set for this bottle, so an agent can't send a session
cookie it was handed for someone else's account.

The jar remembers what each host's `Set-Cookie` responses set, keyed by
(bottle, host). It's in memory and only touched from the asyncio loop.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

# Cookies kept per (bottle, host); past this the oldest set is dropped.
MAX_COOKIES_PER_HOST = 256


def _norm_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def _expired(attributes: Iterable[str]) -> bool:
    """True if the attributes delete the cookie (`Max-Age` <= 0, or an
    `Expires` in the past). `Max-Age` wins over `Expires`."""
    expires = ""
    for attr in attributes:
        key, _, value = attr.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "max-age":
            try:
                return int(value) <= 0
            except ValueError:
                continue
        if key == "expires":
            expires = value
    if not expires:
        return False
    try:
        when = parsedate_to_datetime(expires)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


class CookieJar:
    """What each host set for each bottle: `{(slug, host): {name: value}}`."""

    def __init__(self) -> None:
        self._jars: dict[tuple[str, str], OrderedDict[str, str]] = {}

    def record(self, slug: str, host: str, set_cookies: Iterable[str]) -> None:
        """Remember the cookies a response's `Set-Cookie` headers set (or
        delete)."""
        if not slug:
            return
        key = (slug, _norm_host(host))
        for header in set_cookies:
            first, *attributes = header.split(";")
            name, sep, value = first.partition("=")
            name, value = name.strip(), value.strip()
            if not sep or not name:
                continue
            jar = self._jars.setdefault(key, OrderedDict())
            jar.pop(name, None)
            if _expired(attributes):
                continue
            jar[name] = value
            while len(jar) > MAX_COOKIES_PER_HOST:
                jar.popitem(last=False)

    def filter(self, slug: str, host: str, cookie_headers: Iterable[str]) -> str:
        """The `Cookie` value to send: only the pairs whose name and value
        this host set for this bottle, in the order the agent sent them. ""
        when none are left."""
        jar = self._jars.get((slug, _norm_host(host))) if slug else None
        kept: list[str] = []
        for header in cookie_headers:
            for pair in header.split(";"):
                name, sep, value = pair.strip().partition("=")
                name, value = name.strip(), value.strip()
                if sep and jar is not None and jar.get(name) == value:
                    kept.append(f"{name}={value}")
        return "; ".join(kept)


__all__ = ["MAX_COOKIES_PER_HOST", "CookieJar"]
