"""bot-bottle forge key: an SSH server's keys (ADR 0023).

  bot-bottle forge key add <forge> [<owner/repo> | --default] <path> [--write]
  bot-bottle forge key list [<forge>]
  bot-bottle forge key remove <forge> [<owner/repo> | --default]

A plain SSH git server is a forge of kind `ssh`, connected with
`bot-bottle forge connect ssh://user@host[:port]`. It has no API, so it mints
no keys: this host's connection holds key files instead, one for a repo (a
deploy key) or the forge's default (a user key that reaches many repos). A
grant uses its repo's key, else the default.

A key is kept by reference, its path and fingerprint, never a copy. One
rotated in place is used, and its new fingerprint recorded at the next
launch. A file readable by group or others is refused, as ssh refuses it,
and so are one owned by another user, a public key, and a key with a
passphrase (git-gate can't type it).
`--write` says the key can push: there's no API to ask, so say so when it
can. Two forges may name the same file; removing a key drops only that
forge's reference to it, never the file.
"""

from __future__ import annotations

import os
import sys

from ...forge_connections import (
    REPO_RE, ForgeConnection, ForgeConnectionError, ForgeKey, add_key, connections, remove_key,
)
from ...forge_keys import KeyFileError, check_file, key_path
from ...lib.log import die, info
from ..constants import PROG

_ADD = f"usage: {PROG} forge key add <forge> [<owner/repo> | --default] <path> [--write]"
_REMOVE = f"usage: {PROG} forge key remove <forge> [<owner/repo> | --default]"


def _connections() -> dict[str, ForgeConnection]:
    try:
        return connections()
    except ForgeConnectionError as e:
        die(str(e))


def _servers() -> dict[str, ForgeConnection]:
    return {h: c for h, c in _connections().items() if c.kind == "ssh"}


def _repo(args: list[str], default: bool, usage: str) -> str:
    """The `<owner/repo>` among `args`, or "" with `--default`."""
    if default == bool(args):
        die(usage)
    if args and not REPO_RE.fullmatch(args[0]):
        die(f"{args[0]!r} isn't <owner>/<repo>")
    return args[0] if args else ""


def add(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if flags - {"--default", "--write"} or len(args) not in (2, 3):
        die(_ADD)
    host, path = args[0].lower(), args[-1]
    repo = _repo(args[1:-1], "--default" in flags, _ADD)
    conn = _connections().get(host)
    if conn is None or conn.kind != "ssh":
        die(f"{host} isn't an SSH server connected here; connect it with `{PROG} forge connect "
            f"ssh://git@{host}`" if conn is None else
            f"{host} is a {conn.kind} forge: its keys are minted per launch")
    path = key_path(path, os.getcwd())
    try:
        fingerprint = check_file(path)
        add_key(host, ForgeKey(repo=repo, path=path, fingerprint=fingerprint,
                               write="--write" in flags))
    except (KeyFileError, ForgeConnectionError) as e:
        die(str(e))
    info(f"{host} uses {path} ({fingerprint}) for {repo or 'every repo without its own key'}, "
         f"{'read and write' if '--write' in flags else 'read only (--write if it can push)'}")
    shared = sorted(h for h, c in _servers().items() if h != host
                    and any(k.path == path or k.fingerprint == fingerprint for k in c.keys))
    if shared:
        info(f"the same key is {', '.join(shared)}'s too")
    return 0


def _state(key: ForgeKey) -> str:
    try:
        now = check_file(key.path)
    except KeyFileError as e:
        return f"can't be used: {e}"
    return "ok" if now == key.fingerprint else f"changed: now {now}, recorded at the next launch"


def list_keys(argv: list[str]) -> int:
    if len(argv) > 1:
        die(f"usage: {PROG} forge key list [<forge>]")
    servers = _servers()
    if argv and argv[0].lower() not in servers:
        die(f"{argv[0]} isn't an SSH server connected here (`{PROG} forge list` lists them)")
    shown = {argv[0].lower(): servers[argv[0].lower()]} if argv else servers
    if not shown:
        print(f"no SSH servers connected on this host (`{PROG} forge connect "
              f"ssh://user@host` connects one)")
    for host, conn in shown.items():
        print(f"{host} ({conn.ssh})")
        if not conn.keys:
            print(f"  no keys (`{PROG} forge key add {host} <owner/repo> <path>`)")
        for key in conn.keys:
            print(f"  {key.repo or '(default)':<30} {'write' if key.write else 'read':<5} "
                  f"{key.fingerprint}  {key.path}  {_state(key)}")
    return 0


def remove(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if flags - {"--default"} or len(args) not in (1, 2):
        die(_REMOVE)
    host = args[0].lower()
    repo = _repo(args[1:], "--default" in flags, _REMOVE)
    try:
        key = remove_key(host, repo)
    except ForgeConnectionError as e:
        die(str(e))
    info(f"{host} no longer uses {key.path} for {repo or 'its default'}; the file stays")
    return 0


_ACTIONS = {"add": add, "list": list_keys, "remove": remove}


def cmd_forge_key(argv: list[str]) -> int:
    if not argv or argv[0] not in _ACTIONS:
        sys.stderr.write(__doc__ or "")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    return _ACTIONS[argv[0]](argv[1:])


__all__ = ["cmd_forge_key"]
