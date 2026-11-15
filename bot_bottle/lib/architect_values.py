"""The bottle architect's two settings, as the console and the host both
check them. Here, not in the suggester, so the host's config store can check
a value without it.

- `bottle-architect.host`: which host runs the architect. The account's is
  a host id (as the console gives one at pairing, a UUID) or `off`; an
  unpaired host's own is `local` (itself, the default) or `off`.
- `bottle-architect.connection`: what that host runs it on: `auto`,
  `claude`, `codex` or `pi:<provider>/<model>` (`suggest.architect_provider`
  says what each runs on).

Each is at most 200 characters.
"""

from __future__ import annotations

import re

HOST_SETTING = "bottle-architect.host"
CONNECTION_SETTING = "bottle-architect.connection"
MAX_LENGTH = 200
LOCAL = "local"
OFF = "off"
AUTO = "auto"
_CONNECTION = re.compile(r"^(auto|claude|codex|pi:[A-Za-z0-9._-]+/[A-Za-z0-9._:/@+-]+)$")
# A host id: `local` and `off` are words, never ids.
_HOST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _short(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_LENGTH


def valid_connection(value: object) -> bool:
    """Whether `value` is a `bottle-architect.connection` value."""
    return _short(value) and bool(_CONNECTION.fullmatch(str(value)))


def valid_host_id(value: object) -> bool:
    """Whether `value` has a host id's shape."""
    return (_short(value) and value not in (LOCAL, OFF)
            and bool(_HOST_ID.fullmatch(str(value))))


def valid_account_host(value: object) -> bool:
    """Whether `value` is an account's `bottle-architect.host`: a host id or `off`."""
    return value == OFF or valid_host_id(value)


def valid_local_host(value: object) -> bool:
    """Whether `value` is an unpaired host's own `bottle-architect.host`."""
    return value in (LOCAL, OFF)


def check_connection(value: object) -> str:
    """`value`, if it's valid; else ValueError saying what is."""
    if not valid_connection(value):
        raise ValueError(f"{CONNECTION_SETTING} is auto, claude, codex or "
                         f"pi:<provider>/<model> (at most {MAX_LENGTH} characters)")
    return str(value)


def check_local_host(value: object) -> str:
    """`value`, if it's an unpaired host's own; else ValueError."""
    if not valid_local_host(value):
        raise ValueError(f"{HOST_SETTING} is {LOCAL} (this host) or {OFF} on a host "
                         "that isn't paired")
    return str(value)


def check_account_host(value: object) -> str:
    """`value`, if it's an account's; else ValueError."""
    if not valid_account_host(value):
        raise ValueError(f"{HOST_SETTING} is a host's id, {LOCAL} (this host) or {OFF} "
                         f"(at most {MAX_LENGTH} characters)")
    return str(value)


__all__ = ["AUTO", "CONNECTION_SETTING", "HOST_SETTING", "LOCAL", "MAX_LENGTH", "OFF",
           "check_account_host", "check_connection", "check_local_host", "valid_account_host",
           "valid_connection", "valid_host_id", "valid_local_host"]
