"""bot-bottle forge: connect a forge so bottles can grant its repos in a line.

  bot-bottle forge connect <url> [<bottle>] [<name>]

Checks a token for the forge, pins its SSH host key, and writes the forge into
<bottle> (default: one named after the forge kind, `gitea` or `github`; `./<name>`
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

Gitea and GitHub (github.com, or a GitHub Enterprise Server's URL).

github.com connects through the bot-bottle GitHub App (ADR 0012): install the
app on the accounts and orgs whose repos bottles will grant, then authorize
this host with the code it prints (GitHub's device flow). Bottles act as you,
"you via bot-bottle", only on repos the app is installed on. No token is
pasted; the token GitHub issues is kept in ~/.bot-bottle/secrets and renewed
as it expires.

  bot-bottle forge connect github [<bottle>] [--app SLUG] [--client-id ID]

--app and --client-id name the app (default: bot-bottle; a dev host uses its
own) and are remembered in ~/.bot-bottle/github-app.json. --token connects
github.com with a pasted personal token instead, the fallback.
"""

from __future__ import annotations

import getpass
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from ...contract.forges import unsupported_forge
from ...forge_connect import ConnectError, connect as connect_forge, normalize_url
from ...forge_kinds import detect, forge_api
from ...lib.log import die, info, warn
from ...paths import bot_bottle_root, host_secrets_path
from ..constants import PROG


def _read_token(url: str, kind: str, *, from_stdin: bool, browser: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().strip()
    api = forge_api(kind)
    page = api.token_page(url)
    info(f"make a token at {page}")
    info(f"  {api.TOKEN_HELP}; a dedicated bot account keeps your own account's reach "
         f"out of the bottle")
    if browser:
        webbrowser.open(page)
    return getpass.getpass("paste the token (it isn't shown): ").strip()


def _take(argv: list[str], flag: str) -> tuple[str, list[str]]:
    if flag not in argv:
        return "", argv
    i = argv.index(flag)
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        die(f"{flag} takes a value")
    return argv[i + 1], argv[:i] + argv[i + 2:]


def connect_github(argv: list[str], app_slug: str, client_id: str) -> int:
    """github.com through the bot-bottle GitHub App's device flow."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import AppConfig, app_config, save_app_config
    from ...github_connect import authorize
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if len(args) > 2 or flags - {"--no-browser"}:
        die(f"usage: {PROG} forge connect github [<bottle>] [--app SLUG] [--client-id ID] "
            f"[--no-browser]")
    config = app_config()
    if app_slug or client_id:
        config = AppConfig(slug=app_slug or config.slug, client_id=client_id or config.client_id)
        info(f"remembered the GitHub App ({config.slug}) in {save_app_config(config)}")
    info(f"install {config.slug} on the accounts and orgs whose repos bottles will grant: "
         f"{config.install_url}")

    def show(device: object) -> None:
        uri = getattr(device, "verification_uri", "")
        info(f"authorize this host: enter {getattr(device, 'user_code', '')} at {uri}")
        if "--no-browser" not in flags:
            webbrowser.open(uri)

    try:
        done = authorize(config, on_code=show, bottle=args[1] if len(args) > 1 else "",
                         cwd=Path(os.getcwd()), host_bottles=bot_bottle_root() / "bottles")
    except ConnectError as e:
        die(str(e))
    info(f"connected GitHub as {done.login} via {done.app} ({done.key} in {done.path}; the "
         f"token in {host_secrets_path()} as {done.secret_name}"
         f"{', renewed as it expires' if done.expiring else ''})")
    info(f"grant its repos from a bottle that extends {done.bottle}:")
    info(f"  git-gate:\n    repos: [{done.key}/<owner>/<repo>:write]")
    return 0


def connect(argv: list[str]) -> int:
    app_slug, argv = _take(argv, "--app")
    client_id, argv = _take(argv, "--client-id")
    args = [a for a in argv if not a.startswith("--")]
    if args and args[0] in ("github", "github.com", "https://github.com") and not (
            {"--token", "--token-stdin"} & set(argv)):
        return connect_github(argv, app_slug, client_id)
    argv = [a for a in argv if a != "--token"]
    if args and args[0] == "github":
        argv = ["https://github.com" if a == "github" else a for a in argv]
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
            f"[--token] [--token-stdin] [--no-browser]")
    url = normalize_url(args[0])
    kind = detect(url)
    if not kind:
        die(unsupported_forge(urlparse(url).hostname or url))
    bottle = args[1] if len(args) > 1 else kind
    token = _read_token(url, kind, from_stdin="--token-stdin" in flags,
                        browser="--no-browser" not in flags)
    try:
        done = connect_forge(url, token, bottle=bottle, name=args[2] if len(args) > 2 else "",
                             ssh=ssh_override, cwd=Path(os.getcwd()),
                             host_bottles=bot_bottle_root() / "bottles", kind=kind)
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
