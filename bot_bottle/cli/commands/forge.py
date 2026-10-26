"""bot-bottle forge: connect a forge so bottles can grant its repos in a line.

  bot-bottle forge connect <url> [<bottle>] [<name>]

Checks a token for the forge, pins its SSH host key, and writes the forge into
<bottle> (default: one named after the forge kind, e.g. `gitea`; `./<name>`
for a project bottle), creating it if needed. <name> is the forge's key in
that bottle: its host by default, or <account>@<host> for another account on
it. Bottles that extend <bottle> then grant repos as
`<name>/<owner>/<repo>[:write]`, read-only unless `:write`.

The token is kept in ~/.bot-bottle/secrets, never in the bottle.

Flags:
  --ssh URL       the forge's SSH address, ssh://user@host[:port], when the one
                  it reports isn't reachable from here (e.g. over a VPN)
  --token-stdin   read the token from stdin instead of asking
  --no-browser    print the token page instead of opening it

Only Gitea so far.
"""

from __future__ import annotations

import getpass
import os
import sys
import webbrowser
from pathlib import Path

from ...contrib.gitea import forge_api
from ...forge_connect import ConnectError, connect as connect_forge, normalize_url
from ...lib.log import die, info, warn
from ...paths import bot_bottle_root, host_secrets_path
from ..constants import PROG


def _read_token(url: str, *, from_stdin: bool, browser: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().strip()
    page = forge_api.token_page(url)
    info(f"make a token at {page}")
    info(f"  with scopes {' and '.join(forge_api.TOKEN_SCOPES)}; a dedicated bot account "
         f"keeps your own account's reach out of the bottle")
    if browser:
        webbrowser.open(page)
    return getpass.getpass("paste the token (it isn't shown): ").strip()


def connect(argv: list[str]) -> int:
    ssh_override = ""
    if "--ssh" in argv:
        i = argv.index("--ssh")
        if i + 1 >= len(argv) or not argv[i + 1].startswith("ssh://"):
            die("--ssh takes ssh://user@host[:port]")
        ssh_override = argv[i + 1].rstrip("/")
        argv = argv[:i] + argv[i + 2:]
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if not args or len(args) > 3 or flags - {"--token-stdin", "--no-browser"}:
        die(f"usage: {PROG} forge connect <url> [<bottle>] [<name>] [--ssh URL] "
            f"[--token-stdin] [--no-browser]")
    url = normalize_url(args[0])
    bottle = args[1] if len(args) > 1 else "gitea"
    token = _read_token(url, from_stdin="--token-stdin" in flags,
                        browser="--no-browser" not in flags)
    try:
        done = connect_forge(url, token, bottle=bottle, name=args[2] if len(args) > 2 else "",
                             ssh=ssh_override, cwd=Path(os.getcwd()),
                             host_bottles=bot_bottle_root() / "bottles")
    except ConnectError as e:
        die(str(e))
    info(f"the token is {done.account}'s, admin on {done.admin_repos} of the "
         f"{done.repos} repos it can see; deploy keys can only be added where it's admin")
    if done.host_key:
        info(f"pinned the SSH host key of {done.ssh}: {done.host_key.split()[0]} "
             f"…{done.host_key.split()[-1][-12:]}")
    for line in done.warnings:
        warn(line)
    info(f"wrote {done.key} to {done.path} (token in {host_secrets_path()} as "
         f"{done.secret_name})")
    info(f"grant its repos from a bottle that extends {done.bottle}:")
    info(f"  git-gate:\n    repos: [{done.key}/<owner>/<repo>:write]")
    return 0


def cmd_forge(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] != "connect":
        sys.stderr.write(__doc__ or "")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    return connect(argv[1:])
