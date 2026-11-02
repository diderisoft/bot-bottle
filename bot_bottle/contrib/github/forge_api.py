"""What `bot-bottle forge connect` asks GitHub (github.com, or a GitHub
Enterprise Server at its own URL): is it GitHub, whose token is this, where
do its repos live over SSH and which can the token add deploy keys to."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ...contract.forges import GITHUB_HOSTS
from ...forge_kinds import ForgeApiError, TokenCheck, ssh_base

_TIMEOUT_SECS = 15
# Pages of 100 repos read to count where the token is admin.
_MAX_REPO_PAGES = 10
TOKEN_HELP = ("a fine-grained token for the repos bottles will grant, with Contents: read "
              "(read & write to push) and Metadata: read, plus Administration: read & write "
              "only for a forge with `transport: deploy-key`; a classic token with the repo "
              "scope also works")


def _github_com(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in GITHUB_HOSTS


def api_base(url: str) -> str:
    """The REST API root: api.github.com, or a server's `/api/v3`."""
    if _github_com(url):
        return "https://api.github.com"
    return f"{url.rstrip('/')}/api/v3"


def headers(token: str = "") -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "bot-bottle"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def token_page(url: str) -> str:
    base = "https://github.com" if _github_com(url) else url.rstrip("/")
    return f"{base}/settings/personal-access-tokens/new"


def _get(url: str, token: str = "", timeout: float = _TIMEOUT_SECS) -> object:
    req = urllib.request.Request(url, headers=headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()[:200]
        raise ForgeApiError(f"HTTP {e.code} from {url}: {body}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach {url}: {getattr(e, 'reason', e)}") from e


def is_github(url: str, timeout: float = _TIMEOUT_SECS) -> bool:
    """Whether `url` is github.com, or answers GitHub Enterprise Server's
    `/api/v3/meta`."""
    if _github_com(url):
        return True
    try:
        body = _get(f"{api_base(url)}/meta", timeout=timeout)
    except ForgeApiError:
        return False
    return isinstance(body, dict) and ("installed_version" in body
                                       or "verifiable_password_authentication" in body)


def answers(url: str, timeout: float = _TIMEOUT_SECS) -> bool:
    """`forge_kinds.detect`'s question: is `url` this kind of forge."""
    return is_github(url, timeout)


def check_token(url: str, token: str) -> TokenCheck:
    """Ask GitHub about `token`. Raises ForgeApiError naming what's wrong."""
    api = api_base(url)
    user = _get(f"{api}/user", token)
    if not isinstance(user, dict) or not user.get("login"):
        raise ForgeApiError("GitHub didn't say whose token this is")
    repos: list[object] = []
    for page in range(1, _MAX_REPO_PAGES + 1):
        batch = _get(f"{api}/user/repos?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ForgeApiError("GitHub didn't list the token's repos")
        repos += batch
        if len(batch) < 100:
            break
    ssh = "ssh://git@github.com" if _github_com(url) else next(
        (ssh_base(str(r["ssh_url"])) for r in repos
         if isinstance(r, dict) and r.get("ssh_url")), "")
    admin = sum(1 for r in repos if isinstance(r, dict)
                and isinstance(r.get("permissions"), dict) and r["permissions"].get("admin"))
    return TokenCheck(account=str(user["login"]), ssh=ssh, admin_repos=admin,
                      repos=len(repos))


__all__ = ["ForgeApiError", "TOKEN_HELP", "TokenCheck", "answers", "api_base",
           "check_token", "headers", "is_github", "token_page"]
