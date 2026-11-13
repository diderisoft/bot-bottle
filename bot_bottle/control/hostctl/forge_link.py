"""This host's forge connections for the console (ADR 0021): the account's
Forges list and the bottle editor's repo picker are built from what each
host carrying a connection reports. Metadata only: no token or secret name
leaves the host.

- `forge_status` -> `forge_status` (`forges`: each connection's host, kind,
  url, ssh and users, each user's login, default, app, transport, `state`
  (`ok`, `needs_install`, `expired`, `missing`, `error`) with a `message`,
  and on the GitHub App its installations).
- `forge_repos` (`forge`, `login`?) -> `forge_repos` (`repos`: `full_name`,
  `private`, `permissions` {`pull`, `push`, `admin`}, `installed` (true
  through the GitHub App, which reaches only repos it's installed on; null
  otherwise), `truncated`), or `error`. `login` picks a connected user, else
  the forge's default.

Both ask the forge, page after page, so the link runs them beside its other
traffic (`remote_actions.BESIDE`). An error says only what went wrong (an
HTTP status, unreachable), never the forge's response body.
- `forge_remove` (`forge`, `login`) -> `forge_removed` (`removed`: the users
  removed), or `error`: the console's Disconnect for this host. It drops the
  user's row and its token (with an expiring token's companions) from this
  host, as `bot-bottle forge remove <forge> --user <login>` does. Bottles
  already running keep what they were given until they stop.
- `github_status` -> `github_status`: the host page's GitHub App steps
  (`github_connect.status`).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from ...lib import log

# Enough for an account's repos, without paging a large org's forever.
MAX_REPOS = 1000
# A repo list stops here, marked truncated, however many pages are left.
MAX_SECONDS = 60.0
_GITHUB_PAGE = 100
_GITEA_PAGE = 50


class ForgeLinkError(Exception):
    """A connection or its token couldn't be used; the message says why."""


def _brief(e: Exception) -> str:
    """A forge error as the console may see it: the status, not the body."""
    m = re.search(r"HTTP (\d{3})", str(e))
    if m and m.group(1) == "401":
        return "the forge rejected the token; connect it again"
    return f"the forge answered HTTP {m.group(1)}" if m else "couldn't reach the forge"


def _secrets() -> Any:
    # pylint: disable=import-outside-toplevel
    from ...paths import host_secrets_path
    from ...secret_source import FileSecrets
    return FileSecrets(host_secrets_path())


def _token(kind: str, secret_name: str) -> str | None:
    """A connected user's token: GitHub's App token renewed when due."""
    # pylint: disable-next=import-outside-toplevel
    from ...contrib.github.user_token import current_token
    secrets = _secrets()
    return current_token(secret_name, secrets) if kind == "github" else secrets.get(secret_name)


def _user_state(conn: Any, user: Any) -> dict[str, Any]:
    """One user's reportable state, asking the forge as little as it can."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import ReauthorizeError, installations
    from ...forge_kinds import ForgeApiError
    out: dict[str, Any] = {"login": user.name, "default": user.default, "app": user.app,
                           "transport": user.transport, "state": "ok", "message": ""}
    try:
        token = _token(conn.kind, user.token_secret)
        if not token:
            out.update(state="missing", message="its token isn't on this host; connect it again")
        elif user.app:
            found = installations(token)
            out["installations"] = [{"account": i.get("account", ""),
                                     "repository_selection": i.get("repository_selection", "")}
                                    for i in found]
            if not found:
                out.update(state="needs_install",
                           message=f"the {user.app} GitHub App isn't installed anywhere yet")
    except ReauthorizeError as e:
        out.update(state="expired", message=str(e))
    except ForgeApiError as e:
        out.update(state="error", message=_brief(e))
    return out


def status() -> list[dict[str, Any]]:
    """Every connection on this host, with its users' states."""
    # pylint: disable-next=import-outside-toplevel
    from ...forge_connections import ForgeConnectionError, connections
    try:
        found = connections()
    except ForgeConnectionError as e:
        raise ForgeLinkError(str(e)) from e
    return [{"host": c.host, "kind": c.kind, "url": c.url, "ssh": c.ssh,
             "users": [_user_state(c, u) for u in c.users]}
            for c in sorted(found.values(), key=lambda c: c.host)]


def _get(kind: str, url: str, token: str) -> object:
    # pylint: disable=import-outside-toplevel,protected-access
    if kind == "github":
        from ...contrib.github import forge_api as github
        return github._get(url, token)
    from ...contrib.gitea import forge_api as gitea
    return gitea._get(url, token)


def _repo(raw: object, installed: bool | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("full_name"):
        return None
    found = raw.get("permissions")
    perms: dict[str, object] = found if isinstance(found, dict) else {}
    return {"full_name": str(raw["full_name"]), "private": bool(raw.get("private")),
            "permissions": {k: bool(perms.get(k)) for k in ("pull", "push", "admin")},
            "installed": installed}


def _pages(fetch: Callable[[int], list[object]], deadline: float) -> tuple[list[object], bool]:
    """Pages from 1 until an empty one (a server may send fewer per page than
    asked), up to MAX_REPOS or the deadline; and whether it stopped early."""
    items: list[object] = []
    page = 1
    while len(items) < MAX_REPOS:
        if time.monotonic() > deadline:
            return items, True
        batch = fetch(page)
        if not batch:
            return items, False
        items += batch
        page += 1
    return items[:MAX_REPOS], True


def _listed(body: object, key: str = "") -> list[object]:
    if key:
        body = body.get(key) if isinstance(body, dict) else None
    return body if isinstance(body, list) else []


def _github_repos(conn: Any, user: Any, token: str,
                  deadline: float) -> tuple[list[dict[str, Any]], bool]:
    """Through the App: the repos of its installations the user reaches (a
    user token reaches no other). Otherwise the user's own and shared repos."""
    # pylint: disable-next=import-outside-toplevel
    from ...contrib.github.forge_api import api_base
    api = api_base(conn.url)
    found: dict[str, dict[str, Any]] = {}
    truncated = False
    if user.app:
        insts = _listed(_get("github", f"{api}/user/installations?per_page=100", token),
                        "installations")
        sources = [(f"{api}/user/installations/{i['id']}/repositories?per_page={_GITHUB_PAGE}",
                    "repositories", True) for i in insts if isinstance(i, dict) and i.get("id")]
    else:
        sources = [(f"{api}/user/repos?per_page={_GITHUB_PAGE}"
                    "&affiliation=owner,collaborator,organization_member", "", None)]
    for base, key, installed in sources:
        items, cut = _pages(lambda p, b=base, k=key: _listed(
            _get("github", f"{b}&page={p}", token), k), deadline)
        truncated |= cut
        for raw in items:
            repo = _repo(raw, installed)
            if repo:
                found.setdefault(repo["full_name"].lower(), repo)
    return list(found.values()), truncated


def _gitea_repos(conn: Any, token: str, deadline: float) -> tuple[list[dict[str, Any]], bool]:
    """The user's own and shared repos, and those of each org the user is in
    (which `/user/repos` leaves out)."""
    api = f"{conn.url.rstrip('/')}/api/v1"
    orgs, _ = _pages(lambda p: _listed(
        _get("gitea", f"{api}/user/orgs?limit={_GITEA_PAGE}&page={p}", token)), deadline)
    bases = [f"{api}/user/repos?limit={_GITEA_PAGE}"] + [
        f"{api}/orgs/{o['username']}/repos?limit={_GITEA_PAGE}"
        for o in orgs if isinstance(o, dict) and o.get("username")]
    found: dict[str, dict[str, Any]] = {}
    truncated = False
    for base in bases:
        items, cut = _pages(lambda p, b=base: _listed(_get("gitea", f"{b}&page={p}", token)),
                            deadline)
        truncated |= cut
        for raw in items:
            repo = _repo(raw, None)
            if repo:
                found.setdefault(repo["full_name"].lower(), repo)
    return list(found.values()), truncated


def repos(forge: str, login: str = "") -> dict[str, Any]:
    """The repos `login` (else the forge's default user) can reach on `forge`,
    sorted by name, with permissions; ForgeLinkError saying why not."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import ReauthorizeError
    from ...forge_connections import ForgeConnectionError, get
    from ...forge_kinds import ForgeApiError
    try:
        conn = get(forge)
    except ForgeConnectionError as e:
        raise ForgeLinkError(str(e)) from e
    if conn is None:
        raise ForgeLinkError(f"{forge} isn't connected on this host")
    users = [u for u in conn.users if not login or u.name.lower() == login.lower()]
    user = next((u for u in users if u.default), users[0] if users else None)
    if user is None:
        raise ForgeLinkError(f"{login} isn't connected on {forge} here")
    try:
        token = _token(conn.kind, user.token_secret)
        if not token:
            raise ForgeLinkError(f"this host has no token for {user.name} on {forge}; "
                                 "connect it again")
        deadline = time.monotonic() + MAX_SECONDS
        found, truncated = (_github_repos(conn, user, token, deadline) if conn.kind == "github"
                            else _gitea_repos(conn, token, deadline))
    except ReauthorizeError as e:
        raise ForgeLinkError(str(e)) from e
    except ForgeApiError as e:
        raise ForgeLinkError(_brief(e)) from e
    ordered = sorted(found, key=lambda r: r["full_name"].lower())
    return {"forge": conn.host, "login": user.name, "repos": ordered[:MAX_REPOS],
            "truncated": truncated or len(ordered) > MAX_REPOS}


async def forge_status(msg: Mapping[str, Any]) -> dict[str, Any]:
    reply: dict[str, Any] = {"type": "forge_status", "request_id": msg.get("request_id")}
    try:
        reply["forges"] = await asyncio.to_thread(status)
    except ForgeLinkError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: reading the forges failed: {type(e).__name__}")
        reply["error"] = "reading the forges failed; see the host controller's log"
    return reply


async def forge_repos(msg: Mapping[str, Any]) -> dict[str, Any]:
    reply: dict[str, Any] = {"type": "forge_repos", "request_id": msg.get("request_id")}
    forge = str(msg.get("forge") or "").strip().lower()
    if not forge:
        reply["error"] = "which forge?"
        return reply
    try:
        reply.update(await asyncio.to_thread(repos, forge, str(msg.get("login") or "")))
    except ForgeLinkError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: listing {forge}'s repos failed: {type(e).__name__}")
        reply["error"] = "listing the repos failed; see the host controller's log"
    return reply


async def forge_remove(msg: Mapping[str, Any]) -> dict[str, Any]:
    reply: dict[str, Any] = {"type": "forge_removed", "request_id": msg.get("request_id")}
    forge = str(msg.get("forge") or "").strip().lower()
    login = str(msg.get("login") or "").strip()
    if not forge or not login:
        reply["error"] = "which forge and login?"  # never every user by omission
        return reply
    # pylint: disable-next=import-outside-toplevel
    from ...forge_connections import ForgeConnectionError, get, remove

    def removing() -> list[str]:
        # The login as stored, matched regardless of case (as forge_repos does).
        conn = get(forge)
        stored = next((u.name for u in conn.users if u.name.lower() == login.lower()),
                      login) if conn else login
        return remove(forge, stored)
    try:
        reply["removed"] = await asyncio.to_thread(removing)
    except ForgeConnectionError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: removing {forge} failed: {type(e).__name__}")
        reply["error"] = "removing it failed; see the host controller's log"
    else:
        log.info(f"remote: removed {login} from {forge}")
    return reply


async def github_status(msg: Mapping[str, Any]) -> dict[str, Any]:
    """The app this host's GitHub access goes through, a flow in progress, and
    who's connected with which installations."""
    # pylint: disable-next=import-outside-toplevel
    from ...github_connect import status as github
    reply: dict[str, Any] = {"type": "github_status", "request_id": msg.get("request_id")}
    try:
        reply.update(await asyncio.to_thread(github))
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: reading GitHub's status failed: {type(e).__name__}")
        reply["error"] = "reading GitHub's status failed; see the host controller's log"
    return reply


REQUESTS = {"forge_status": forge_status, "forge_repos": forge_repos,
            "forge_remove": forge_remove, "github_status": github_status}

__all__ = ["ForgeLinkError", "MAX_REPOS", "MAX_SECONDS", "REQUESTS", "forge_remove", "forge_repos",
           "forge_status",
           "github_status", "repos", "status"]
