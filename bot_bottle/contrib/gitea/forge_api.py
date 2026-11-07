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


def _headers(token: str) -> dict[str, str]:
    return {"Accept": "application/json", **({"Authorization": f"token {token}"} if token else {})}


def _api(url: str) -> str:
    return f"{url.rstrip('/')}/api/v1"

def _send(method: str, url: str, token: str, body: dict[str, object] | None = None
          ) -> tuple[int, object]:
    """(status, JSON body) for one request; a status that isn't 2xx is
    returned, not raised, so callers can tell 404 from failure. ForgeApiError
    when the forge can't be reached."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **_headers(token), **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace").strip()
        try:
            return e.code, json.loads(text) if text else {}
        except ValueError:
            return e.code, {"message": text[:200]}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach {url}: {getattr(e, 'reason', e)}") from e


def _message(body: object) -> str:
    return str(body.get("message", "")) if isinstance(body, dict) else ""


def repo_exists(url: str, token: str, owner: str, name: str) -> bool:
    """Whether `owner/name` exists (that the token can see)."""
    status, body = _send("GET", f"{_api(url)}/repos/{owner}/{name}", token)
    if status == 404:
        return False
    if status >= 300:
        raise ForgeApiError(f"HTTP {status} asking about {owner}/{name}: {_message(body)}")
    return True


def create_repo(url: str, token: str, *, owner: str, name: str, private: bool,
                as_user: bool) -> str:
    """Create the empty repo `owner/name` (under the token's own account when
    `as_user`, else the org `owner`); its web URL. ForgeApiError saying why
    the forge refused."""
    where = f"{_api(url)}/user/repos" if as_user else f"{_api(url)}/orgs/{owner}/repos"
    status, body = _send("POST", where, token,
                         {"name": name, "private": private, "auto_init": False})
    if status >= 300:
        raise ForgeApiError(f"the forge refused to create {owner}/{name} "
                            f"(HTTP {status}): {_message(body) or 'no reason given'}")
    return str(body.get("html_url", "")) if isinstance(body, dict) else ""


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
           "check_token", "create_repo", "is_gitea", "repo_exists", "ssh_base", "token_page"]
