"""bot-bottle forge: connect a forge so bottles can grant its repos in a line.

  bot-bottle forge connect <url> [--default] [--ssh URL] [--transport T]

Checks a token for the forge, pins its SSH host key, and records the
connection in ~/.bot-bottle/forges.md under the user the token belongs to
(ADR 0019). Any bottle then grants the forge's repos as
`<host>/<owner>/<repo>[:write]`, read-only unless `:write`, with nothing to
extend. Connecting another user's token adds that user; the forge's first
user is its default, and an agent picks another with
`forge_users: {<host>: <user>}`.

The token is kept in ~/.bot-bottle/secrets, never in a bottle.

Flags:
  --default       make this user the forge's default
  --ssh URL       the forge's SSH address, ssh://user@host[:port], when the one
                  it reports isn't reachable from here (e.g. over a VPN)
  --transport T   GitHub only: `https` (the token, the default) or `deploy-key`
                  (a key per repo; the token must be able to add them)
  --token-stdin   read the token from stdin instead of asking
  --no-browser    print the token page instead of opening it

Gitea and GitHub (github.com, or a GitHub Enterprise Server's URL).

github.com connects through the bot-bottle GitHub App (ADR 0012): install the
app on the accounts and orgs whose repos bottles will grant, then authorize
this host with the code it prints (GitHub's device flow). Bottles act as you,
"you via bot-bottle", only on repos the app is installed on. No token is
pasted; the token GitHub issues is kept in ~/.bot-bottle/secrets and renewed
as it expires.

  bot-bottle forge connect github [--app SLUG] [--client-id ID] [--default]
                                  [--transport T] [--owner ACCOUNT] [--reauthorize]
                                  [--no-wait]

--app and --client-id name the app (default: bot-bottle; a dev host uses its
own) and are remembered in ~/.bot-bottle/github-app.json. A host already
connected isn't authorized again unless --reauthorize. Then, unless the app
is installed (on --owner, when named), it opens the install page and waits
for the installation (--no-wait just prints where). --transport sets how
git-gate reaches repos as this user, as above; on a host connected already it
changes just that. --token connects
github.com with a pasted personal token instead, the fallback.
"""

from __future__ import annotations

import getpass
import sys
import webbrowser
from urllib.parse import urlparse

from ...contract.forges import unsupported_forge
from ...forge_connect import ConnectError, connect as connect_forge, normalize_url
from ...forge_kinds import detect, forge_api
from ...lib import prompt
from ...lib.log import die, info, warn
from ...paths import host_secrets_path
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
    asker = prompt.current()  # a full-screen view asks on its screen
    if asker is not None:
        return asker.ask("paste the token (it isn't shown)", secret=True).strip()
    return getpass.getpass("paste the token (it isn't shown): ").strip()


def _take(argv: list[str], flag: str) -> tuple[str, list[str]]:
    if flag not in argv:
        return "", argv
    i = argv.index(flag)
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        die(f"{flag} takes a value")
    return argv[i + 1], argv[:i] + argv[i + 2:]


_TRANSPORT_NOTE = {
    "https": "repos are reached over https: the token is in the gateway for a run",
    "deploy-key": "repos are reached over deploy keys: minting one needs the app to "
                  "have Administration on the repo, or the launch fails saying so",
}


def connect_github(argv: list[str], app_slug: str, client_id: str, owner: str = "",
                   transport: str = "") -> int:
    """github.com through the bot-bottle GitHub App: authorize this host with
    the device flow (unless it already is), then make sure the app is
    installed, waiting for it. `transport` sets how git-gate reaches its
    repos as this user (ADR 0014), for a user connected already too."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import AppConfig, app_config, save_app_config
    from ...forge_connections import TRANSPORTS
    from ...github_connect import authorize, connected_token, set_transport
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if len(args) > 1 or flags - {"--no-browser", "--no-wait", "--reauthorize", "--default"}:
        die(f"usage: {PROG} forge connect github [--app SLUG] [--client-id ID] [--default] "
            f"[--transport T] [--owner ACCOUNT] [--reauthorize] [--no-wait] [--no-browser]")
    if transport and transport not in TRANSPORTS:
        die(f"--transport takes {' or '.join(TRANSPORTS)}")
    config = app_config()
    if app_slug or client_id:
        config = AppConfig(slug=app_slug or config.slug, client_id=client_id or config.client_id)
        info(f"remembered the GitHub App ({config.slug}) in {save_app_config(config)}")
    browser = "--no-browser" not in flags
    existing = None if "--reauthorize" in flags else connected_token()
    if existing is not None:
        login, token = existing
        info(f"GitHub is connected as {login} via {config.slug} (--reauthorize to do it again)")
        if transport:
            try:
                set_transport(login, transport)
            except ConnectError as e:
                die(str(e))
            info(f"{login} now reaches repos over {transport}")
    else:
        def show(device: object) -> None:
            uri = getattr(device, "verification_uri", "")
            info(f"authorize this host: enter {getattr(device, 'user_code', '')} at {uri}")
            if browser:
                webbrowser.open(uri)

        try:
            done = authorize(config, on_code=show, make_default="--default" in flags,
                             transport=transport)
        except ConnectError as e:
            die(str(e))
        info(f"connected GitHub as {done.login} via {done.app} (in {done.path}; the "
             f"token in {host_secrets_path()} as {done.secret_name}"
             f"{', renewed as it expires' if done.expiring else ''})")
        info(_TRANSPORT_NOTE[transport or "https"])
        info("grant its repos from any bottle:")
        info(f"  git-gate:\n    repos: [{done.key}/<owner>/<repo>:write]")
        got = connected_token(done.secret_name)
        if got is None:
            die("GitHub was connected, but its token can't be read back; connect it again")
        token = got[1]
    _install_step(config, token, owner=owner, wait="--no-wait" not in flags, browser=browser)
    return 0


def _install_step(config: object, token: str, *, owner: str, wait: bool,
                  browser: bool) -> None:
    """The app's installations, or where to install it and (unless `wait` is
    off) waiting until it is: on `owner` when named, else anywhere."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import installations
    from ...forge_kinds import ForgeApiError
    from ...github_connect import describe, installed, wait_for_installation
    slug, url = getattr(config, "slug", ""), getattr(config, "install_url", "")
    try:
        found = installations(token)
        if installed(found, owner):
            for inst in found:
                info(f"{slug} is installed on {describe(token, inst)}")
            info(f"to add another account or org: {url}")
            return
        info(f"install {slug} on {owner or 'the accounts and orgs whose repos bottles will grant'}"
             f": {url}")
        if not wait:
            return
        if browser:
            webbrowser.open(url)
        info("waiting for the installation (Ctrl-C to stop waiting)")
        found = wait_for_installation(token, owner=owner)
    except ForgeApiError as e:
        die(str(e))
    except KeyboardInterrupt:
        info(f"stopped waiting; bottles reach a repo once {slug} is installed on its owner")
        return
    if found is None:
        warn(f"{slug} isn't installed yet; install it at {url}")
        return
    for inst in found:
        info(f"installed on {describe(token, inst)}")


def connect(argv: list[str]) -> int:
    app_slug, argv = _take(argv, "--app")
    client_id, argv = _take(argv, "--client-id")
    owner, argv = _take(argv, "--owner")
    transport, argv = _take(argv, "--transport")
    args = [a for a in argv if not a.startswith("--")]
    if args and args[0] in ("github", "github.com", "https://github.com") and not (
            {"--token", "--token-stdin"} & set(argv)):
        return connect_github(argv, app_slug, client_id, owner, transport)
    argv = [a for a in argv if a != "--token"]
    if args and args[0] == "github":
        argv = ["https://github.com" if a == "github" else a for a in argv]
    ssh_override, argv = _take(argv, "--ssh")
    if ssh_override and not ssh_override.startswith("ssh://"):
        die("--ssh takes ssh://user@host[:port]")
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1 or flags - {"--token-stdin", "--no-browser", "--default"}:
        die(f"usage: {PROG} forge connect <url> [--default] [--ssh URL] [--transport T] "
            f"[--token] [--token-stdin] [--no-browser]")
    url = normalize_url(args[0])
    kind = detect(url)
    if not kind:
        die(unsupported_forge(urlparse(url).hostname or url))
    token = _read_token(url, kind, from_stdin="--token-stdin" in flags,
                        browser="--no-browser" not in flags)
    try:
        done = connect_forge(url, token, ssh=ssh_override.rstrip("/"), kind=kind,
                             make_default="--default" in flags, transport=transport)
    except ConnectError as e:
        die(str(e))
    info(f"the token is {done.account}'s, admin on {done.admin_repos} of the "
         f"{done.repos} repos it can see; deploy keys can only be added where it's admin")
    if done.host_key:
        info(f"pinned the SSH host key of {done.ssh}: {done.host_key.split()[0]} "
             f"…{done.host_key.split()[-1][-12:]}")
    for line in done.warnings:
        warn(line)
    info(f"connected {done.key} as {done.account}{' (the default user)' if done.default else ''}"
         f" in {done.path} (token in {host_secrets_path()} as {done.secret_name})")
    info("grant its repos from any bottle:")
    info(f"  git-gate:\n    repos: [{done.key}/<owner>/<repo>:write]")
    return 0


def cmd_forge(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] != "connect":
        sys.stderr.write(__doc__ or "")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    return connect(argv[1:])
