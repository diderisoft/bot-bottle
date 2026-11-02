"""GitHub through the shared `bot-bottle` GitHub App's user access tokens
(ADR 0012): the operator installs the app on their accounts and orgs, then
authorizes this host with GitHub's device flow. The token acts as the
operator ("<user> via bot-bottle"), limited to the repos the app is installed
on and the operator can reach, and only to the app's permissions.

The app's private key appears nowhere in bot-bottle: device-flow tokens are
issued and refreshed with the app's public client id alone. An expiring token
(eight hours) is refreshed here, under a lock, before it's used; its refresh
token rotates on every refresh and lasts six months. Tokens live in the host
secrets file and are never logged."""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

from ...forge_kinds import ForgeApiError

GITHUB = "https://github.com"
API = "https://api.github.com"
DEFAULT_SLUG = "bot-bottle"
CONFIG_FILENAME = "github-app.json"
_TIMEOUT_SECS = 30
# Refreshed this long before it would expire, so a launch never starts on a
# token that dies mid-provisioning.
_REFRESH_MARGIN_SECS = 600
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# Companions of a token's secret, for an expiring one.
REFRESH_SUFFIX, EXPIRES_SUFFIX, CLIENT_ID_SUFFIX = "_REFRESH", "_EXPIRES", "_CLIENT_ID"
REAUTHORIZE = ("re-authorize GitHub: `bot-bottle forge connect github`, or Connect GitHub on "
               "the console's host page")


class ReauthorizeError(ForgeApiError):
    """The authorization is gone (refresh token expired or revoked)."""


@dataclass(frozen=True)
class AppConfig:
    """Which app this host's GitHub access goes through. Both are public."""

    slug: str
    client_id: str

    @property
    def install_url(self) -> str:
        return install_url(self.slug)


def install_url(slug: str) -> str:
    return f"{GITHUB}/apps/{slug}/installations/new"


def app_config(root: Path | None = None) -> AppConfig:
    """`<root>/github-app.json` (each named environment has its own), else the
    `bot-bottle` app, whose client id must be set before authorizing."""
    # pylint: disable-next=import-outside-toplevel
    from ...paths import bot_bottle_root
    stored: dict[str, object] = {}
    try:
        raw = json.loads(((root or bot_bottle_root()) / CONFIG_FILENAME).read_text())
        stored = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        pass
    return AppConfig(slug=str(stored.get("slug") or DEFAULT_SLUG),
                     client_id=str(stored.get("client_id") or ""))


def save_app_config(config: AppConfig, root: Path | None = None) -> Path:
    # pylint: disable-next=import-outside-toplevel
    from ...paths import bot_bottle_root
    path = (root or bot_bottle_root()) / CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"slug": config.slug, "client_id": config.client_id},
                               indent=2) + "\n")
    return path


def _post_form(url: str, fields: dict[str, str]) -> dict[str, object]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Accept": "application/json",
                                          "User-Agent": "bot-bottle"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ForgeApiError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach {url}: {getattr(e, 'reason', e)}") from e
    if not isinstance(body, dict):
        raise ForgeApiError(f"{url} didn't answer with an object")
    return body


@dataclass(frozen=True)
class DeviceCode:
    """A device flow in progress: the operator enters `user_code` at
    `verification_uri`; `device_code` stays on the host."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


def start_device(client_id: str) -> DeviceCode:
    if not client_id:
        raise ForgeApiError("this host doesn't know the GitHub App's client id; set it with "
                            "`bot-bottle forge connect github --client-id <id>`")
    body = _post_form(f"{GITHUB}/login/device/code", {"client_id": client_id})
    if body.get("error"):
        raise ForgeApiError(f"GitHub refused the device flow: {body.get('error')} "
                            f"(is device flow enabled on the app?)")
    try:
        return DeviceCode(device_code=str(body["device_code"]), user_code=str(body["user_code"]),
                          verification_uri=str(body["verification_uri"]),
                          expires_in=int(str(body.get("expires_in", 900))),
                          interval=int(str(body.get("interval", 5))))
    except (KeyError, ValueError) as e:
        raise ForgeApiError("GitHub's device code answer was incomplete") from e


@dataclass(frozen=True)
class TokenSet:
    """A user access token, and for an expiring one its refresh token and
    when it expires (unix seconds)."""

    access: str
    refresh: str = ""
    expires_at: int = 0


def _token_set(body: dict[str, object], now: float) -> TokenSet:
    access = str(body.get("access_token") or "")
    if not access:
        raise ForgeApiError("GitHub didn't return a token")
    expires_in = int(str(body.get("expires_in") or 0))
    return TokenSet(access=access, refresh=str(body.get("refresh_token") or ""),
                    expires_at=int(now) + expires_in if expires_in else 0)


def wait_for_token(client_id: str, device: DeviceCode, *,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.time,
                   cancelled: Callable[[], bool] = lambda: False) -> TokenSet:
    """Poll until the operator approves, at GitHub's pace. ForgeApiError when
    they deny it, it expires, or `cancelled()`."""
    interval = max(1, device.interval)
    deadline = clock() + device.expires_in
    while clock() < deadline and not cancelled():
        sleep(interval)
        body = _post_form(f"{GITHUB}/login/oauth/access_token",
                          {"client_id": client_id, "device_code": device.device_code,
                           "grant_type": _DEVICE_GRANT})
        error = str(body.get("error") or "")
        if not error:
            return _token_set(body, clock())
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise ForgeApiError("the authorization was declined on GitHub")
        if error == "expired_token":
            break
        raise ForgeApiError(f"GitHub refused the authorization: {error}")
    raise ForgeApiError("the code expired before it was entered; start again")


def refresh(client_id: str, refresh_token: str, now: float | None = None) -> TokenSet:
    """A new token pair. No client secret: tokens from the device flow are
    refreshed with the client id alone."""
    body = _post_form(f"{GITHUB}/login/oauth/access_token",
                      {"client_id": client_id, "grant_type": "refresh_token",
                       "refresh_token": refresh_token})
    if body.get("error"):
        raise ReauthorizeError(f"GitHub refused to renew the authorization "
                               f"({body.get('error')}); {REAUTHORIZE}")
    return _token_set(body, time.time() if now is None else now)


def store(path: Path, name: str, tokens: TokenSet, client_id: str) -> None:
    """Keep `tokens` under `name` in the secrets file at `path`, with their
    companions when they expire (and none when they don't)."""
    # pylint: disable-next=import-outside-toplevel
    from ...secret_source import remove_secret, write_secret
    write_secret(path, name, tokens.access)
    if tokens.refresh:
        write_secret(path, name + REFRESH_SUFFIX, tokens.refresh)
        write_secret(path, name + EXPIRES_SUFFIX, str(tokens.expires_at))
        write_secret(path, name + CLIENT_ID_SUFFIX, client_id)
    else:
        for suffix in (REFRESH_SUFFIX, EXPIRES_SUFFIX, CLIENT_ID_SUFFIX):
            remove_secret(path, name + suffix)


@contextlib.contextmanager
def _locked(path: Path) -> Generator[None]:
    """One refresh at a time: the refresh token rotates, so a second use of
    the old one would fail."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".github-lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def current_token(name: str, secrets: object, *, path: Path | None = None,
                  now: float | None = None) -> str | None:
    """The token in secret `name`, renewed first when it's about to expire
    (an expiring token's companions are in the host secrets file). None when
    there's no such secret. ReauthorizeError when it can't be renewed."""
    # pylint: disable=import-outside-toplevel
    from ...paths import host_secrets_path
    from ...secret_source import FileSecrets
    token = secrets.get(name)  # type: ignore[attr-defined]
    if token is None:
        return None
    t = time.time() if now is None else now
    expires = secrets.get(name + EXPIRES_SUFFIX)  # type: ignore[attr-defined]
    if not expires or not expires.isdigit() or int(expires) - _REFRESH_MARGIN_SECS > t:
        return token
    path = path or host_secrets_path()
    with _locked(path):
        fresh = FileSecrets(path)  # another launch may have renewed it meanwhile
        expires = fresh.get(name + EXPIRES_SUFFIX) or "0"
        if expires.isdigit() and int(expires) - _REFRESH_MARGIN_SECS > t:
            return fresh.get(name) or token
        refresh_token = fresh.get(name + REFRESH_SUFFIX) or ""
        client_id = fresh.get(name + CLIENT_ID_SUFFIX) or ""
        if not refresh_token or not client_id:
            raise ReauthorizeError(f"the GitHub authorization expired; {REAUTHORIZE}")
        tokens = refresh(client_id, refresh_token, t)
        store(path, name, tokens, client_id)
        return tokens.access


def _get(url: str, token: str) -> object:
    # pylint: disable-next=import-outside-toplevel
    from .forge_api import headers
    req = urllib.request.Request(url, headers=headers(token))
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
        return json.loads(resp.read())


def login(token: str) -> str:
    """Whose token this is."""
    try:
        body = _get(f"{API}/user", token)
    except urllib.error.HTTPError as e:
        raise ForgeApiError(f"HTTP {e.code} from GitHub asking whose token this is") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach GitHub: {getattr(e, 'reason', e)}") from e
    if not isinstance(body, dict) or not body.get("login"):
        raise ForgeApiError("GitHub didn't say whose token this is")
    return str(body["login"])


def installations(token: str) -> list[dict[str, str]]:
    """The app's installations this token reaches: account and selection."""
    try:
        body = _get(f"{API}/user/installations?per_page=100", token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ReauthorizeError(f"GitHub rejected the token; {REAUTHORIZE}") from e
        raise ForgeApiError(f"HTTP {e.code} from GitHub listing installations") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach GitHub: {getattr(e, 'reason', e)}") from e
    items = body.get("installations") if isinstance(body, dict) else None
    out: list[dict[str, str]] = []
    for inst in items if isinstance(items, list) else ():
        account = inst.get("account") if isinstance(inst, dict) else None
        if isinstance(inst, dict):
            out.append({"id": str(inst.get("id", "")),
                        "account": str(account.get("login", "")) if isinstance(account, dict)
                        else "",
                        "repository_selection": str(inst.get("repository_selection", ""))})
    return out


def selected_count(token: str, installation_id: str) -> int | None:
    """How many repos an installation with selected repos covers, or None
    when GitHub can't say."""
    try:
        body = _get(f"{API}/user/installations/{installation_id}/repositories?per_page=1",
                    token)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    count = body.get("total_count") if isinstance(body, dict) else None
    return count if isinstance(count, int) else None


def reachable(token: str, owner_repo: str) -> bool:
    """Whether the token reaches `owner_repo` (the app is installed there and
    the operator can see it). ReauthorizeError when the token is rejected."""
    try:
        _get(f"{API}/repos/{owner_repo}", token)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return False
        if e.code == 401:
            raise ReauthorizeError(f"GitHub rejected the token; {REAUTHORIZE}") from e
        raise ForgeApiError(f"HTTP {e.code} from GitHub about {owner_repo}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ForgeApiError(f"couldn't reach GitHub: {getattr(e, 'reason', e)}") from e
    return True


__all__ = ["AppConfig", "DEFAULT_SLUG", "DeviceCode", "REAUTHORIZE", "ReauthorizeError",
           "TokenSet", "app_config", "current_token", "install_url", "installations", "login",
           "reachable", "refresh", "save_app_config", "selected_count", "start_device", "store",
           "wait_for_token"]
