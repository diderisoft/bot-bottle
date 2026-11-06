"""`bot-bottle forge connect` (PRD 0103, ADR 0019): check a forge token, pin
the forge's SSH host key, and record the connection in `~/.bot-bottle/forges.md`
(`forge_connections`).

The forge is Gitea or GitHub (`forge_kinds`). The token goes to the host
secrets file; `forges.md` names it under the user the forge says owns it.
Bottles then grant the forge's repos as `<host>/<owner>/<repo>[:write]`
without stacking anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import forge_alerts
from .forge_connections import (
    ForgeConnection, ForgeUser, add_user, forges_path, get as get_connection, host_of,
    secret_name as user_secret_name,
)
from .manifest.util import ManifestError


def connected_secret(host: str, user: str) -> str:
    """The secret name `user`'s token on `host` is kept under: the one it
    has if connected already, else `<HOST>_TOKEN` for a forge's first user,
    `<USER>_<HOST>_TOKEN` for another."""
    conn = get_connection(host)
    for u in conn.users if conn else ():
        if u.name == user:
            return u.token_secret
    return user_secret_name(host, user, first=not (conn and conn.users))


def record(conn: ForgeConnection, user: ForgeUser, *,
           make_default: bool = False) -> ForgeConnection:
    """Write `conn` with `user` connected on it, as ConnectError when
    `forges.md` won't take it."""
    try:
        return add_user(conn, user, make_default=make_default)
    except (ManifestError, OSError) as e:
        raise ConnectError(f"{forges_path()}: {e}") from e


Keyscan = Callable[[str, str], str]


def ssh_host_port(ssh: str) -> tuple[str, str]:
    """`ssh://user@host[:port]` -> (host, port)."""
    hostport = ssh[len("ssh://"):].split("@", 1)[-1]
    host, _, port = hostport.partition(":")
    return host, port or "22"


class ConnectError(Exception):
    """The forge couldn't be connected; the message says why."""


@dataclass(frozen=True)
class Connected:
    """A forge connected: where it was written and what the token can do."""

    key: str            # the forge's host, a grant's prefix
    path: Path
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
    and record the user it belongs to in `forges.md`. `kind` skips
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
        raise ConnectError(f"{e}; give an SSH address this host can reach, "
                           f"e.g. ssh://git@<ip>:<port>") from e
    key = host_of(url)
    secret = connected_secret(key, check.account)
    write_secret(host_secrets_path(), secret, token.strip())
    forge_alerts.clear(secret)  # a refused token, replaced
    conn = record(ForgeConnection(host=key, kind=kind, url=url, ssh=ssh, host_key=host_key),
                  ForgeUser(name=check.account, token_secret=secret, transport=transport),
                  make_default=make_default)
    return Connected(key=key, path=forges_path(), account=check.account,
                     default=conn.user().name == check.account,
                     admin_repos=check.admin_repos, repos=check.repos, ssh=ssh,
                     host_key=host_key, secret_name=secret, warnings=tuple(warnings))


__all__ = ["ConnectError", "Connected", "connect", "connected_secret", "normalize_url",
           "record", "ssh_host_port"]
