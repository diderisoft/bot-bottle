"""`bot-bottle config`: the bottle architect's settings, by name.

  config                 list them, with the values in force here
  config get KEY         one setting's value here
  config set KEY VALUE   set it
  config unset KEY       reset this host's own value to its default

Settings:

  bottle-architect.host        which host runs the bottle architect. One
                               host per account does; others suggest with
                               the rules. Paired, it's your account's: a
                               host's id, local (this host) or off, and
                               setting it asks the console. Not paired:
                               local (the default) or off.
  bottle-architect.connection  what the architect host runs it on: auto
                               (the default: claude, then codex, whichever
                               is installed and logged in there), claude,
                               codex or pi:<provider>/<model> (e.g.
                               pi:openrouter/qwen/qwen3-coder:free, with
                               your pi config and key for that provider).

On a paired host the settings are the account's, so `unset` is refused and
`set` needs the console link. `bot-bottle status` shows the architect in use.
They're in the host DB, so they're read and set through the host controller.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ...control.hostctl.client import HostControlRefused
from ...control.hostctl.runner import HostControlError
from ...lib.architect_values import CONNECTION_SETTING, HOST_SETTING, check_connection
from .. import host_state_client as host
from ..constants import PROG

# Each setting's name -> its field in `POST /architect` and its answer.
KEYS = {HOST_SETTING: "host", CONNECTION_SETTING: "connection"}


def _value(found: dict[str, Any], key: str) -> str:
    return str(found.get(KEYS[key], "")) or "(none)"


def _list(_ns: argparse.Namespace) -> int:
    found = host.architect()
    for key in sorted(KEYS):
        sys.stdout.write(f"{key} = {_value(found, key)}\n")
    return 0


def _get(ns: argparse.Namespace) -> int:
    sys.stdout.write(f"{_value(host.architect(), ns.key)}\n")
    return 0


def _set(ns: argparse.Namespace) -> int:
    if ns.action == "set" and ns.key == CONNECTION_SETTING:
        try:
            check_connection(ns.value)
        except ValueError as e:
            sys.stderr.write(f"{PROG} config set: {e}\n")
            return 2
    body: dict[str, object] = ({KEYS[ns.key]: ns.value} if ns.action == "set"
                               else {"unset": KEYS[ns.key]})
    sys.stdout.write(f"{ns.key} = {_value(host.set_architect(body), ns.key)}\n")
    return 0


def cmd_config(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} config", description=(__doc__ or "").split("\n", maxsplit=1)[0],
        epilog=f"settings: {', '.join(sorted(KEYS))}")
    sub = parser.add_subparsers(dest="action")
    get = sub.add_parser("get", help="one setting's value here")
    get.add_argument("key", choices=sorted(KEYS))
    get.set_defaults(run=_get)
    put = sub.add_parser("set", help="set a setting (paired: the account's, via the console)")
    put.add_argument("key", choices=sorted(KEYS))
    put.add_argument("value")
    put.set_defaults(run=_set)
    unset = sub.add_parser("unset", help="reset this host's own value (not while paired)")
    unset.add_argument("key", choices=sorted(KEYS))
    unset.set_defaults(run=_set)
    ns = parser.parse_args(argv)
    try:
        return getattr(ns, "run", _list)(ns)
    except HostControlRefused as e:
        sys.stderr.write(f"{PROG} config: {e.error}\n")
    except HostControlError as e:
        sys.stderr.write(f"{PROG} config: {e}\n")
    return 1


__all__ = ["KEYS", "cmd_config"]
