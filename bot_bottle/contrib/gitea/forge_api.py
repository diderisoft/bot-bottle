"""What `bot-bottle forge connect` asks a Gitea instance (PRD 0103): is it
Gitea, whose token is this, and where do its repos live over SSH and which
can the token add deploy keys to."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ...forge_kinds import ForgeApiError, TokenCheck, ssh_base

_TIMEOUT_SECS = 15
# What a token needs: adding deploy keys, and reading who it belongs to.
TOKEN_SCOPES = ("write:repository", "read:user")
TOKEN_HELP = (f"scopes {' and '.join(TOKEN_SCOPES)}, from an account that's admin on the "
              f"repos bottles will grant (a dedicated bot account is safest)")


def token_page(url: str) -> str:
    return f"{url.rstrip('/')}/user/settings/applications"


def _get(url: str, token: str = "", timeout: float = _TIMEOUT_SECS) -> object:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()[:200]
        raise ForgeApiError(f"HTTP {e.code} from {url}: {body}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach {url}: {getattr(e, 'reason', e)}") from e


def is_gitea(url: str, timeout: float = _TIMEOUT_SECS) -> bool:
    """Whether `url` answers Gitea's version endpoint."""
    try:
        body = _get(f"{url.rstrip('/')}/api/v1/version", timeout=timeout)
    except ForgeApiError:
        return False
    return isinstance(body, dict) and "version" in body


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


def answers(url: str, timeout: float = _TIMEOUT_SECS) -> bool:
    """`forge_kinds.detect`'s question: is `url` this kind of forge."""
    return is_gitea(url, timeout)


__all__ = ["ForgeApiError", "TOKEN_HELP", "TOKEN_SCOPES", "TokenCheck", "answers",
           "check_token", "is_gitea", "ssh_base", "token_page"]
