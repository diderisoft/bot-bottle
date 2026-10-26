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

from ...approvals import trust_project_bottle
from ...contrib.gitea import forge_api
from ...forge_connect import (
    ForgeEntry, bottle_path, default_key, existing_forges, ssh_host_port, write_forge,
)
from ...git_gate.host_key import fetch_host_key
from ...lib.log import die, info, warn
from ...manifest.util import ManifestError
from ...manifest.versions import record_or_warn
from ...paths import bot_bottle_root, host_secrets_path
from ...secret_source import write_secret
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
    url = args[0] if "://" in args[0] else f"https://{args[0]}"
    url = url.rstrip("/")
    if not forge_api.is_gitea(url):
        die(f"{url} doesn't answer as Gitea; only Gitea forges can be connected so far")
    kind = "gitea"
    bottle = args[1] if len(args) > 1 else kind
    try:
        path, project = bottle_path(bottle, host_bottles=bot_bottle_root() / "bottles",
                                    cwd=Path(os.getcwd()))
    except ManifestError as e:
        die(str(e))

    token = _read_token(url, from_stdin="--token-stdin" in flags,
                        browser="--no-browser" not in flags)
    if not token:
        die("no token given")
    try:
        check = forge_api.check_token(url, token)
    except forge_api.ForgeApiError as e:
        die(f"the token didn't work: {e}\n"
            f"it needs the scopes {' and '.join(forge_api.TOKEN_SCOPES)}")
    info(f"the token is {check.account}'s, admin on {check.admin_repos} of the "
         f"{check.repos} repos it can see; deploy keys can only be added where it's admin")
    if not check.admin_repos:
        warn(f"{check.account} can't add a deploy key anywhere yet; give it admin on the "
             f"repos bottles will grant")

    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    ssh = ssh_override or check.ssh or f"ssh://git@{host}"
    try:
        host_key = fetch_host_key(*ssh_host_port(ssh))
        info(f"pinned the SSH host key of {ssh}: {host_key.split()[0]} "
             f"…{host_key.split()[-1][-12:]}")
    except RuntimeError as e:
        host_key = ""
        warn(f"couldn't fetch the SSH host key of {ssh} ({e}); a launch will ask for it. "
             f"If the forge's SSH is reachable another way, connect again with --ssh")

    key = args[2] if len(args) > 2 else default_key(url, check.account, existing_forges(path))
    entry = ForgeEntry(key=key, kind=kind, url=url, ssh=ssh, account=check.account,
                       host_key=host_key)
    write_secret(host_secrets_path(), entry.secret_name, token)
    try:
        write_forge(path, entry)
    except ManifestError as e:
        die(f"{path}: {e}")
    if project:
        trust_project_bottle(Path(os.getcwd()), path.stem)
    else:
        record_or_warn(f"forge connect: {key} in {path.stem}")
    info(f"wrote {key} to {path} (token in {host_secrets_path()} as {entry.secret_name})")
    info(f"grant its repos from a bottle that extends {bottle}:")
    info(f"  git-gate:\n    repos: [{key}/<owner>/<repo>:write]")
    return 0


def cmd_forge(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] != "connect":
        sys.stderr.write(__doc__ or "")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    return connect(argv[1:])
