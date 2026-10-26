"""What `bot-bottle forge connect` asks a Gitea instance (PRD 0103): is it
Gitea, whose token is this, and where do its repos live over SSH and which
can the token add deploy keys to."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

_TIMEOUT_SECS = 15
# What a token needs: adding deploy keys, and reading who it belongs to.
TOKEN_SCOPES = ("write:repository", "read:user")


class ForgeApiError(Exception):
    """The forge couldn't be reached or refused the token."""


@dataclass(frozen=True)
class TokenCheck:
    """Who a token is, the SSH address of its repos (`ssh://user@host[:port]`),
    and how many it can add deploy keys to (it must be admin on a repo)."""

    account: str
    ssh: str
    admin_repos: int
    repos: int


def token_page(url: str) -> str:
    return f"{url.rstrip('/')}/user/settings/applications"


def _get(url: str, token: str = "") -> object:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()[:200]
        raise ForgeApiError(f"HTTP {e.code} from {url}: {body}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach {url}: {getattr(e, 'reason', e)}") from e


def is_gitea(url: str) -> bool:
    """Whether `url` answers Gitea's version endpoint."""
    try:
        body = _get(f"{url.rstrip('/')}/api/v1/version")
    except ForgeApiError:
        return False
    return isinstance(body, dict) and "version" in body


def ssh_base(ssh_url: str) -> str:
    """A repo's SSH URL without the repo: `ssh://git@h:2222/o/r.git` or
    `git@h:o/r.git` -> `ssh://git@h:2222` / `ssh://git@h`."""
    if ssh_url.startswith("ssh://"):
        rest = ssh_url[len("ssh://"):]
        return "ssh://" + rest.split("/", 1)[0]
    user_host = ssh_url.split(":", 1)[0]
    return f"ssh://{user_host}"


def check_token(url: str, token: str) -> TokenCheck:
    """Ask the forge about `token`. Raises ForgeApiError naming what's wrong."""
    api = f"{url.rstrip('/')}/api/v1"
    user = _get(f"{api}/user", token)
    if not isinstance(user, dict) or not user.get("login"):
        raise ForgeApiError("the forge didn't say whose token this is")
    repos = _get(f"{api}/user/repos?limit=50", token)
    if not isinstance(repos, list):
        raise ForgeApiError("the forge didn't list the token's repos")
    ssh = next((ssh_base(str(r["ssh_url"])) for r in repos
                if isinstance(r, dict) and r.get("ssh_url")), "")
    admin = sum(1 for r in repos if isinstance(r, dict)
                and isinstance(r.get("permissions"), dict) and r["permissions"].get("admin"))
    return TokenCheck(account=str(user["login"]), ssh=ssh, admin_repos=admin,
                      repos=len(repos))


__all__ = ["ForgeApiError", "TOKEN_SCOPES", "TokenCheck", "check_token", "is_gitea",
           "ssh_base", "token_page"]
