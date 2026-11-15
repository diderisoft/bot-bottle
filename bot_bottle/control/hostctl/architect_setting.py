"""Which host runs the bottle architect, and on what (PRD 0134):
`bottle-architect.host` and `bottle-architect.connection`.

One declared architect host per account runs it; nothing implies one.

- Paired, the account's values are in force: the console sends them as
  `architect` (`host`, `connection`, and that host's `label` if it has one)
  on each connect and whenever they change; they're kept in the host DB, so
  they hold across restarts and while offline (`received`). `host` is a
  host id, `off`, or `""` when the account hasn't declared one. This host
  runs the architect only when `host` is its own id.
- Unpaired, the host's own values are: `host` `local` (the default: it runs
  the architect) or `off`.
- `bot-bottle config set` on a paired host asks the console
  (`architect.set`, `request`) and reports the account's values once the
  console sends them back; it never writes the account's copy itself.
- Adoption: a paired host told `host: ""` whose own value is `local` offers
  itself once per connect (`received`); the console takes the first offer.

`report` is `host_stats.architect`: `host`, `connection`, `is_architect`,
`ready`, and `runs_on` (the harness) or `reason`. `GET /architect` adds
`label` for `bot-bottle status`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from ...lib import log
from ...lib.architect_values import (
    AUTO, CONNECTION_SETTING, HOST_SETTING, LOCAL, MAX_LENGTH, OFF, check_account_host,
    check_connection, check_local_host, valid_account_host, valid_connection,
)
from ...lib.config_store import ArchitectValues, ConfigStore, HostConfig
from ...paths import host_db_path
from ...suggest.architect_provider import resolve

MESSAGE = "architect"
SET_MESSAGE = "architect.set"
# How long `config set` waits for the console to send the account's values back.
ECHO_SECONDS = 10.0
UNREACHABLE = "can't reach the console; the architect setting is the account's"
OFF_REASON = "the bottle architect is off"
NO_HOST = (f"No architect host: choose one in Settings, or "
           f"`bot-bottle config set {HOST_SETTING} <host>`.")
PAIRED_UNSET = ("this host is paired, so the architect setting is the account's: set it "
                "(`bot-bottle config set ...`) instead")
# `POST /architect`'s fields -> the `HostConfig` field each sets while unpaired.
_FIELDS = {"host": "architect_host", "connection": "architect_connection"}


class Unreachable(Exception):
    """The console couldn't be asked, or didn't answer."""


@dataclass(frozen=True)
class InForce:
    """The architect host and connection in force here; whether this host
    is the architect, and why not (`reason`) when it isn't."""

    host: str
    connection: str
    is_architect: bool
    reason: str = ""
    label: str = ""  # the architect host's, when it's another


def _store() -> ConfigStore:
    return ConfigStore(host_db_path())


def _own_id() -> str | None:
    """This host's id on the console, or None when it isn't paired."""
    # pylint: disable-next=import-outside-toplevel
    from .pairing import RemoteError, load_pairing
    try:
        return load_pairing().require().host_id
    except (RemoteError, sqlite3.Error):
        return None


def _values(store: ConfigStore) -> tuple[ArchitectValues, ArchitectValues]:
    try:
        return store.architect()
    except sqlite3.Error as e:
        log.warn(f"bottle-architect: couldn't read its settings ({e}); using the defaults")
        return ArchitectValues(LOCAL, AUTO), ArchitectValues("", AUTO)


def in_force(store: ConfigStore | None = None) -> InForce:
    """The values in force here: the account's while paired, else this host's."""
    own, account = _values(store or _store())
    me = _own_id()
    if me is None:
        if own.host == OFF:
            return InForce(OFF, own.connection, False, OFF_REASON)
        return InForce(LOCAL, own.connection, True)
    host, connection = account.host, account.connection
    if me and host == me:
        return InForce(host, connection, True)
    if host == OFF:
        return InForce(OFF, connection, False, OFF_REASON)
    if not host:
        return InForce("", connection, False, NO_HOST)
    name = account.label or host
    return InForce(host, connection, False, f"The architect host is {name}; suggestions "
                                            "from other hosts reach it once routing ships.",
                   label=name)


def suggest_args() -> tuple[str, str]:
    """What `suggest.flow.suggest` takes from the setting: the connection
    (`provider`), and why this host doesn't run the architect (`not_here`,
    "" when it does)."""
    found = in_force()
    return found.connection, "" if found.is_architect else found.reason


def report(store: ConfigStore | None = None, *, label: bool = False) -> dict[str, Any]:
    """What `host_stats.architect` says; with `label`, also how `bot-bottle
    status` shows it (`this host, auto → claude`, `delphi`)."""
    found = in_force(store)
    out: dict[str, Any] = {"host": found.host, "connection": found.connection,
                           "is_architect": found.is_architect}
    if found.is_architect:
        resolved = resolve(found.connection)
        out["ready"] = resolved.ready
        if resolved.ready:
            out["runs_on"] = resolved.harness
        else:
            out["reason"] = resolved.reason
        shown = f"this host, {resolved.label}"
    else:
        out.update(ready=False, reason=found.reason)
        shown = {OFF: "off", "": "none"}.get(found.host, found.label)
    if label:
        out["label"] = shown
    return out


def status(_body: dict[str, object] | None = None) -> dict[str, Any]:
    """`GET /architect`: `report`, with its `label`."""
    return report(label=True)


def _label(raw: object) -> str:
    return raw if isinstance(raw, str) and len(raw) <= MAX_LENGTH and raw.isprintable() else ""


def received(msg: dict[str, Any],
             store: ConfigStore | None = None) -> tuple[bool, dict[str, Any] | None]:
    """Keep the account's values from an `architect` message (an invalid
    host is `""`, an invalid connection `auto`). Whether they changed, and
    the `architect.set` adoption to send: this host offering itself when the
    account has no architect host and its own value is `local`."""
    store = store or _store()
    account = ArchitectValues(
        str(msg["host"]) if valid_account_host(msg.get("host")) else "",
        str(msg["connection"]) if valid_connection(msg.get("connection")) else AUTO,
        _label(msg.get("label")))
    try:
        own, before = store.architect()
        changed = before != account
        if changed:
            store.set_architect_account(account)
    except sqlite3.Error as e:
        log.warn(f"bottle-architect: couldn't keep the account's settings ({e})")
        return False, None
    _echoed()
    me = _own_id()
    adopt = ({"type": SET_MESSAGE, "host": me, "connection": own.connection}
             if not account.host and own.host == LOCAL and me else None)
    return changed, adopt


class LinkArchitect:
    """One console connection's `architect` messages: kept (`received`),
    reported at once when they changed (`push`), and this host offered once
    per connect when the account has no architect host."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 push: Callable[[], Awaitable[None]]) -> None:
        self._send, self._push = send, push
        self._offered = False

    async def handle(self, msg: dict[str, Any]) -> None:
        changed, adopt = await asyncio.to_thread(received, msg)
        if changed:
            await self._push()
        if adopt is not None and not self._offered:
            self._offered = True
            await self._send(adopt)


# --- the console link, for `request` ---

@dataclass(frozen=True)
class _Link:
    loop: asyncio.AbstractEventLoop
    send: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


_echo = threading.Condition()
_state: dict[str, Any] = {"link": None, "echoes": 0}


def attach(send: Callable[[dict[str, Any]], Coroutine[Any, Any, None]]) -> object:
    """The connected console's `send`, from inside its loop, so `request`
    can reach it; the token `detach` takes."""
    link = _Link(asyncio.get_running_loop(), send)
    with _echo:
        _state["link"] = link
    return link


def detach(token: object) -> None:
    with _echo:
        if _state["link"] is token:
            _state["link"] = None


def _echoed() -> None:
    with _echo:
        _state["echoes"] += 1
        _echo.notify_all()


def request(changes: dict[str, str]) -> None:
    """Send `architect.set` with `changes` and wait (`ECHO_SECONDS`) for the
    console's next `architect`. Unreachable when the link isn't up or
    nothing comes back."""
    wait = ECHO_SECONDS
    with _echo:
        link, seen = _state["link"], _state["echoes"]
    if not isinstance(link, _Link):
        raise Unreachable(UNREACHABLE)
    try:
        asyncio.run_coroutine_threadsafe(
            link.send({"type": SET_MESSAGE, **changes}), link.loop).result(wait)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the caller
        raise Unreachable(f"{UNREACHABLE} ({e or type(e).__name__})") from e
    with _echo:
        if not _echo.wait_for(lambda: _state["echoes"] > seen, wait):
            raise Unreachable(f"the console didn't answer within {wait:.0f}s; "
                              "the architect setting is the account's")


def change(body: dict[str, object]) -> dict[str, Any]:
    """`POST /architect`: `{host?, connection?}` sets, `{unset: "host" |
    "connection"}` resets this host's own value. Unpaired, it writes this
    host's values; paired, it asks the console (`request`) and `unset` is
    refused. Answers with `status`."""
    unknown = set(body) - {*_FIELDS, "unset"}
    if unknown or not body or ("unset" in body and len(body) > 1):
        raise ValueError("expected host and/or connection, or unset")
    me = _own_id()
    if "unset" in body:
        name = body["unset"]
        if name not in _FIELDS:
            raise ValueError("unset takes host or connection")
        if me is not None:
            raise ValueError(PAIRED_UNSET)
        _store().set_host_config(HostConfig(), [_FIELDS[str(name)]])  # its default
        return status()
    changes: dict[str, str] = {}
    if "connection" in body:
        changes["connection"] = check_connection(body["connection"])
    if "host" in body:
        host = body["host"]
        if me is None:
            changes["host"] = check_local_host(host)
        elif host == LOCAL:
            if not me:
                raise ValueError("this host has no id from the console yet; pair it again")
            changes["host"] = me
        else:
            changes["host"] = check_account_host(host)
    if me is None:
        config = HostConfig(architect_host=changes.get("host", LOCAL),
                            architect_connection=changes.get("connection", AUTO))
        _store().set_host_config(config, [_FIELDS[n] for n in changes])
    else:
        request(changes)
    return status()


__all__ = ["CONNECTION_SETTING", "ECHO_SECONDS", "HOST_SETTING", "InForce", "LinkArchitect",
           "MESSAGE",
           "NO_HOST", "OFF_REASON", "PAIRED_UNSET", "SET_MESSAGE", "UNREACHABLE", "Unreachable",
           "attach", "change", "detach", "in_force", "received", "report", "request", "status",
           "suggest_args"]
