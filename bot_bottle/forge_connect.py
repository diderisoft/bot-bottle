"""`bot-bottle forge connect` (PRD 0103, ADR 0019): check a forge token, pin
the forge's SSH host key, and record the connection in the host DB
(`forge_connections`).

The forge is Gitea or GitHub (`forge_kinds`). The token goes to the host
secrets file; the connection names it under the user the forge says owns it.
Bottles then grant the forge's repos as `<host>/<owner>/<repo>[:write]`
without stacking anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from . import forge_alerts
from .forge_connections import (
    ForgeConnection, ForgeUser, add_user, connect_server, connections, host_of,
    secret_name as user_secret_name, ssh_host,
)
from .manifest.util import ManifestError


def connected_secret(host: str, user: str) -> str:
    """The secret name `user`'s token on `host` is kept under: the one it
    has if connected already, else `BB_HOST_FORGE_<HOST>__<USER>_TOKEN`.
    ConnectError when another connection's token has that name already."""
    name = user_secret_name(host, user)
    for conn in connections().values():
        for u in conn.users:
            if (conn.host, u.name) == (host, user):
                return u.token_secret
            if u.token_secret == name:
                raise ConnectError(
                    f"{u.name} on {conn.host} keeps its token as {name}, the name "
                    f"{user} on {host} would take; disconnect it first "
                    f"(bot-bottle forge remove {conn.host} --user {u.name})")
    return name


def record(conn: ForgeConnection, user: ForgeUser, *,
           make_default: bool = False) -> ForgeConnection:
    """Record `conn` with `user` connected on it, as ConnectError when the
    host won't take it."""
    try:
        return add_user(conn, user, make_default=make_default)
    except (ManifestError, OSError) as e:
        raise ConnectError(f"couldn't record the connection: {e}") from e


Keyscan = Callable[[str, str], str]


def ssh_host_port(ssh: str) -> tuple[str, str]:
    """`ssh://user@host[:port]` -> (host, port)."""
    hostport = ssh[len("ssh://"):].split("@", 1)[-1]
    host, _, port = hostport.partition(":")
    return host, port or "22"


class ConnectError(Exception):
    """The forge couldn't be connected; the message says why."""


class SshUnreachable(ConnectError):
    """The forge's SSH address didn't answer from here: `ssh` is the one
    tried, and `reason` what went wrong, for asking for another."""

    def __init__(self, ssh: str, reason: str) -> None:
        super().__init__(f"{reason}; give an SSH address this host can reach, "
                         f"e.g. ssh://git@<ip>:<port>")
        self.ssh = ssh
        self.reason = reason


@dataclass(frozen=True)
class Connected:
    """A forge connected, and what the token can do."""

    key: str            # the forge's host, a grant's prefix
    account: str
    default: bool
    admin_repos: int
    repos: int
    ssh: str
    host_key: str
    secret_name: str
    warnings: tuple[str, ...] = ()


def normalize_url(url: str) -> str:
    url = url.strip()
    return (url if "://" in url else f"https://{url}").rstrip("/")


def connect(url: str, token: str, *, ssh: str = "", kind: str = "",
            make_default: bool = False, transport: str = "") -> Connected:
    """`bot-bottle forge connect`, for the CLI and the console alike: check the
    forge is a kind bot-bottle connects (Gitea, GitHub) and the token works,
    pin the forge's SSH host key, keep the token in the host secrets file,
    and record the user it belongs to. `kind` skips
    recognizing the forge again; `transport` is GitHub's (ADR 0014). Raises
    ConnectError saying what's wrong. The token is never logged or returned."""
    # pylint: disable=import-outside-toplevel
    from .forge_connections import TRANSPORTS
    from .contract.forges import unsupported_forge
    from .forge_kinds import ForgeApiError, detect, forge_api
    from .git_gate.host_key import fetch_host_key
    from .paths import host_secrets_path
    from .secret_source import write_secret

    url = normalize_url(url)
    if url.startswith("ssh://"):
        raise ConnectError(f"an SSH server is connected on its host, with the key files "
                           f"there: `bot-bottle forge connect {url}`, then `bot-bottle forge "
                           f"key add`")
    if not url.startswith("https://") and not url.startswith("http://"):
        raise ConnectError(f"{url} isn't an http(s) URL")
    if ssh and not ssh.startswith("ssh://"):
        raise ConnectError("the SSH address is ssh://user@host[:port]")
    if not token.strip():
        raise ConnectError("no token given")
    kind = kind or detect(url)
    if not kind:
        raise ConnectError(unsupported_forge(urlparse(url).hostname or url))
    if transport and (kind != "github" or transport not in TRANSPORTS):
        raise ConnectError(f"--transport is GitHub's: {' or '.join(TRANSPORTS)}")
    api = forge_api(kind)
    try:
        check = api.check_token(url, token.strip())
    except ForgeApiError as e:
        raise ConnectError(f"the token didn't work: {e}; it needs {api.TOKEN_HELP}") from e
    warnings: list[str] = []
    # GitHub reaches its repos over HTTPS with the token (ADR 0014): no keys.
    if not check.admin_repos and (kind != "github" or transport == "deploy-key"):
        warnings.append(f"{check.account} can't add a deploy key anywhere yet; give it "
                        f"admin on the repos bottles will grant")
    host = urlparse(url).hostname or ""
    ssh = ssh.rstrip("/") or check.ssh or f"ssh://git@{host}"
    try:
        host_key = fetch_host_key(*ssh_host_port(ssh))
    except RuntimeError as e:
        # Without SSH, git-gate can't reach the forge's repos at all.
        raise SshUnreachable(ssh, str(e)) from e
    key = host_of(url)
    secret = connected_secret(key, check.account)
    write_secret(host_secrets_path(), secret, token.strip())
    forge_alerts.clear(secret)  # a refused token, replaced
    conn = record(ForgeConnection(host=key, kind=kind, url=url, ssh=ssh, host_key=host_key),
                  ForgeUser(name=check.account, token_secret=secret, transport=transport),
                  make_default=make_default)
    return Connected(key=key, account=check.account,
                     default=conn.user().name == check.account,
                     admin_repos=check.admin_repos, repos=check.repos, ssh=ssh,
                     host_key=host_key, secret_name=secret, warnings=tuple(warnings))


_SSH_URL = re.compile(r"^ssh://[^@/\s:]+@[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")


def _check_host_key(host_key: str) -> None:
    """A host key given by hand is one public key, `<type> <base64>` as a
    known_hosts line has it, whose base64 holds a key of that type: it's
    written into git-gate's known_hosts as it is."""
    # pylint: disable-next=import-outside-toplevel
    from .forge_keys import public_fingerprint
    try:
        if len(host_key.split()) != 2 or host_key != host_key.strip():
            raise ValueError("not two fields")
        public_fingerprint(host_key)
    except ValueError as e:
        raise ConnectError("a host key is `<type> <base64>`, one public key as a known_hosts "
                           "line has it") from e


def connect_ssh(ssh: str, host_key: str = "") -> ForgeConnection:
    """`bot-bottle forge connect ssh://user@host[:port]`: a plain SSH git
    server, a forge of kind `ssh` (ADR 0023), keyed by its host. Pins the
    host key it finds, as connecting any forge does (or `host_key`, given
    instead: a server this host can't reach yet), and keeps the keys already
    added; they're added with `forge key add`. ConnectError saying what's
    wrong."""
    # pylint: disable-next=import-outside-toplevel
    from .git_gate.host_key import fetch_host_key
    ssh = ssh.strip().rstrip("/")
    if not _SSH_URL.fullmatch(ssh):
        raise ConnectError(f"an SSH server is ssh://user@host[:port], with no path "
                           f"(was {ssh!r})")
    host_port = ssh_host_port(ssh)
    if not 0 < int(host_port[1]) < 65536:
        raise ConnectError(f"{host_port[1]} isn't a port (was {ssh!r})")
    if host_key:
        _check_host_key(host_key)
    try:
        host_key = host_key or fetch_host_key(*host_port)
    except RuntimeError as e:
        raise SshUnreachable(ssh, str(e)) from e
    found = connections().get(ssh_host(ssh))
    if found is not None and found.kind != "ssh":
        raise ConnectError(f"{found.host} is connected as a {found.kind} forge; remove it "
                           f"first (bot-bottle forge remove {found.host})")
    try:
        return connect_server(ForgeConnection(host=ssh_host(ssh), kind="ssh", url="",
                                              ssh=ssh, host_key=host_key))
    except (ManifestError, OSError) as e:
        raise ConnectError(f"couldn't record the connection: {e}") from e


__all__ = ["ConnectError", "Connected", "SshUnreachable", "connect", "connect_ssh",
           "connected_secret", "normalize_url", "record", "ssh_host_port"]
