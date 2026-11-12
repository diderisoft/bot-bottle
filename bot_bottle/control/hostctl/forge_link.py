"""This host's forge connections for the console (ADR 0021): the account's
Forges list and the bottle editor's repo picker are built from what each
host carrying a connection reports. Metadata only: no token or secret name
leaves the host.

- `forge_status` -> `forge_status` (`forges`: each connection's host, kind,
  url, ssh and users, each user's login, default, app, transport, `state`
  (`ok`, `needs_install`, `expired`, `missing`, `error`) with a `message`,
  and on the GitHub App its installations).
- `forge_repos` (`forge`, `login`?) -> `forge_repos` (`repos`: `full_name`,
  `private`, `permissions` {`pull`, `push`, `admin`}, `installed` (GitHub
  App: whether the App reaches it; null otherwise), `truncated`), or `error`.
  `login` picks a connected user, else the forge's default.
- `github_status` -> `github_status`: the host page's GitHub App steps
  (`github_connect.status`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from ...lib import log

# Enough for an account's repos, without paging a large org's forever.
MAX_REPOS = 1000
_GITHUB_PAGE = 100
_GITEA_PAGE = 50


class ForgeLinkError(Exception):
    """A connection or its token couldn't be used; the message says why."""


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
        out.update(state="error", message=str(e))
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


def _pages(fetch: Callable[[int], list[object]], size: int) -> tuple[list[object], bool]:
    """Pages from 1 until a short one, up to MAX_REPOS; and whether it stopped short."""
    items: list[object] = []
    page = 1
    while len(items) < MAX_REPOS:
        batch = fetch(page)
        items += batch
        if len(batch) < size:
            return items, False
        page += 1
    return items[:MAX_REPOS], True


def _github_repos(conn: Any, user: Any, token: str) -> tuple[list[dict[str, Any]], bool]:
    # pylint: disable-next=import-outside-toplevel
    from ...contrib.github.forge_api import api_base
    api = api_base(conn.url)

    def listed(body: object, key: str = "") -> list[object]:
        if key:
            body = body.get(key) if isinstance(body, dict) else None
        return body if isinstance(body, list) else []

    reached: dict[str, dict[str, Any]] = {}
    truncated = False
    if user.app:
        insts = listed(_get("github", f"{api}/user/installations?per_page=100", token),
                       "installations")
        for inst in insts:
            if not isinstance(inst, dict) or not inst.get("id"):
                continue
            base = f"{api}/user/installations/{inst['id']}/repositories?per_page={_GITHUB_PAGE}"
            items, cut = _pages(lambda p, b=base: listed(_get("github", f"{b}&page={p}", token),
                                                         "repositories"), _GITHUB_PAGE)
            truncated |= cut
            for raw in items:
                repo = _repo(raw, True)
                if repo:
                    reached[repo["full_name"].lower()] = repo
    mine = (f"{api}/user/repos?per_page={_GITHUB_PAGE}"
            "&affiliation=owner,collaborator,organization_member")
    items, cut = _pages(lambda p: listed(_get("github", f"{mine}&page={p}", token)), _GITHUB_PAGE)
    truncated |= cut
    for raw in items:
        repo = _repo(raw, False if user.app else None)
        if repo and repo["full_name"].lower() not in reached:
            reached[repo["full_name"].lower()] = repo
    return list(reached.values()), truncated


def _gitea_repos(conn: Any, token: str) -> tuple[list[dict[str, Any]], bool]:
    base = f"{conn.url.rstrip('/')}/api/v1/user/repos?limit={_GITEA_PAGE}"

    def fetch(page: int) -> list[object]:
        body = _get("gitea", f"{base}&page={page}", token)
        return body if isinstance(body, list) else []

    items, truncated = _pages(fetch, _GITEA_PAGE)
    return [r for r in (_repo(raw, None) for raw in items) if r], truncated


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
        found, truncated = (_github_repos(conn, user, token) if conn.kind == "github"
                            else _gitea_repos(conn, token))
    except ReauthorizeError as e:
        raise ForgeLinkError(str(e)) from e
    except ForgeApiError as e:
        raise ForgeLinkError(str(e)) from e
    return {"forge": conn.host, "login": user.name,
            "repos": sorted(found, key=lambda r: r["full_name"].lower()),
            "truncated": truncated}


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
            "github_status": github_status}

__all__ = ["ForgeLinkError", "MAX_REPOS", "REQUESTS", "forge_repos", "forge_status",
           "github_status", "repos", "status"]
