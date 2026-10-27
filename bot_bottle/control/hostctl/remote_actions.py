"""Console requests about this host's credentials, answered over the link:
refreshing a running agent's forwarded host login, and connecting a forge
(PRD 0105). Each returns the reply to send."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...lib import log


async def refresh_credentials(msg: Mapping[str, Any], running: Mapping[str, str],
                              bottles: Any) -> dict[str, Any]:
    """Re-read a running agent's forwarded host login now and hand it to the
    gateway (`credential_refresh`)."""
    agent_id = str(msg.get("agent_id", ""))
    bottle = running.get(agent_id)
    refresh = getattr(bottles, "refresh_credentials", None)  # BottleService's
    result: dict[str, object] = (
        {"error": "that agent isn't running on this host"} if not bottle
        else {"error": "this host controller can't refresh credentials"} if refresh is None
        else await asyncio.to_thread(refresh, bottle))
    return {"type": "credentials_refreshed", "request_id": msg.get("request_id"),
            "agent_id": agent_id, **result}


def _text(msg: Mapping[str, Any], key: str, default: str = "") -> str:
    value = msg.get(key, default)
    return value if isinstance(value, str) else default


async def forge_connect(msg: Mapping[str, Any]) -> dict[str, Any]:
    """Connect a forge from the console (`forge_connect.connect`), into a host
    bottle. The token arrives in the message and goes only to the forge and
    the host secrets file: never into the reply or a log."""
    # pylint: disable=import-outside-toplevel
    from ...forge_connect import ConnectError, connect
    from ...paths import bot_bottle_root
    reply: dict[str, Any] = {"type": "forge_connected", "request_id": msg.get("request_id")}
    bottle = _text(msg, "bottle", "gitea") or "gitea"
    if bottle.startswith("./"):
        reply["error"] = "the console connects forges into host bottles, not a project's"
        return reply
    try:
        done = await asyncio.to_thread(
            connect, _text(msg, "url"), _text(msg, "token"), bottle=bottle,
            name=_text(msg, "name"), ssh=_text(msg, "ssh"), cwd=Path.home(),
            host_bottles=bot_bottle_root() / "bottles")
    except ConnectError as e:
        reply["error"] = str(e)
        return reply
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: connecting a forge failed: {type(e).__name__}")
        reply["error"] = "connecting failed; see the host controller's log"
        return reply
    log.info(f"remote: connected {done.key} as {done.account} into bottle {done.bottle}")
    reply.update(key=done.key, bottle=done.bottle, account=done.account,
                 admin_repos=done.admin_repos, repos=done.repos, ssh=done.ssh,
                 host_key=done.host_key, warnings=list(done.warnings))
    return reply


def _connected_hosts() -> set[str]:
    """The hostnames of the forges this host's own bottles declare."""
    # pylint: disable=import-outside-toplevel
    from urllib.parse import urlparse
    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
    from ...paths import bot_bottle_root
    hosts: set[str] = set()
    for path in sorted((bot_bottle_root() / "bottles").glob("*.md")):
        try:
            fm, _ = parse_frontmatter(path.read_text())
        except (OSError, YamlSubsetError):
            continue
        git = fm.get("git-gate")
        forges = git.get("forges") if isinstance(git, dict) else None
        for entry in forges.values() if isinstance(forges, dict) else ():
            if isinstance(entry, dict):
                hosts.add(urlparse(str(entry.get("url", ""))).hostname or "")
    return hosts - {""}


def forge_attention(request: Any) -> tuple[list[str], list[str]]:
    """What the phone shows for a `forge-connect` request, and its option. The
    URL is the agent's, so the card says when this host has never connected
    it: a token pasted for the wrong host is sent to that host."""
    # pylint: disable=import-outside-toplevel
    from urllib.parse import urlparse
    from ...contrib.gitea.forge_api import TOKEN_SCOPES, token_page
    url = request.proposed.strip()
    lines = [f"forge: {url}"]
    if request.justification.strip():
        lines.append(f"why: {request.justification.strip()}")
    if (urlparse(url).hostname or "") not in _connected_hosts():
        lines.append("new to this host")
    lines += [f"token page: {token_page(url)}", f"scopes: {', '.join(TOKEN_SCOPES)}"]
    return lines, ["forge_connect"]


async def forge_connect_answer(msg: Mapping[str, Any], asking: Mapping[str, Any],
                               orchestrator: Any, _bottles: Any = None) -> dict[str, Any]:
    """The phone's answer to an agent's `forge-connect` request: connect the
    forge the *agent* named (never a URL from the console) with the token the
    operator pasted, then approve the request with a note the agent reads. A
    failed connect leaves the request waiting."""
    # pylint: disable=import-outside-toplevel
    from ...contract.supervise import TOOL_FORGE_CONNECT
    from ...util import same_instant
    from ..client import AlreadyAnswered
    from .nudge import nudge_if_waited
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "forge_connect_answered",
                             "request_id": msg.get("request_id"), "agent_id": agent_id}
    request = asking.get(agent_id)
    if (request is None or request.tool != TOOL_FORGE_CONNECT
            or not same_instant(_text(msg, "since"), request.arrived)):
        reply["error"] = "That request is no longer waiting."
        return reply
    result = await forge_connect({"url": request.proposed, "token": _text(msg, "token"),
                                  "bottle": _text(msg, "bottle", "gitea"),
                                  "ssh": _text(msg, "ssh")})
    if result.get("error"):
        reply["error"] = result["error"]
        return reply
    notes = (f"connected {result['key']} as {result['account']}; ask for its repos with "
             f"add-repo")
    try:
        await asyncio.to_thread(orchestrator(request.orchestrator).supervise_respond,
                                request.id, bottle_slug=request.bottle, decision="approve",
                                notes=notes)
        await asyncio.to_thread(nudge_if_waited, request, "approved", notes)
    except AlreadyAnswered:
        result["warnings"] = [*result.get("warnings", []),
                              "the request was answered elsewhere meanwhile"]
    reply.update({k: v for k, v in result.items() if k not in ("type", "request_id")})
    return reply


def repo_attention(request: Any) -> tuple[list[str], list[str]]:
    """What the phone shows for an `add-repo` request: the repo, the access
    asked for (the operator may lower write to read), and where keeping it
    would write."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import keep_target
    from .repo_grants import find_forge
    grant = request.proposed.strip()
    writable = grant.endswith(":write")
    repo = grant.removesuffix(":write")
    lines = [f"repo: {repo}", f"access: {'write' if writable else 'read-only'}"]
    if request.justification.strip():
        lines.append(f"why: {request.justification.strip()}")
    found = find_forge(request.session, repo.split("/", 1)[0])
    if found is None:
        lines.append("forge not connected on this host: connect it, then the agent can ask again")
        return lines, []
    target = keep_target(request.session)
    options = ["add_repo", *(["add_repo_write"] if writable else [])]
    if target is not None:
        lines.append(f"keep: in {target.label()}")
        options.append("add_repo_keep")
    return lines, options


def card(request: Any) -> tuple[list[str], list[str]] | None:
    """The phone card for a request that takes more than allow/deny, or None."""
    # pylint: disable-next=import-outside-toplevel
    from ...contract.supervise import TOOL_ADD_REPO, TOOL_FORGE_CONNECT
    if request.tool == TOOL_FORGE_CONNECT:
        return forge_attention(request)
    if request.tool == TOOL_ADD_REPO:
        return repo_attention(request)
    return None


async def add_repo_answer(msg: Mapping[str, Any], asking: Mapping[str, Any],
                          orchestrator: Any, bottles: Any) -> dict[str, Any]:
    """The phone's grant for an agent's `add-repo` request: grant the repo the
    agent named to its running bottle (read-only unless the operator chose
    write and the agent asked for it), keep it in the bottle if asked, then
    approve with a note giving the URL. A failed grant leaves it waiting."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import keep_grant, keep_target
    from ...contract.supervise import TOOL_ADD_REPO
    from ...util import same_instant
    from ..client import AlreadyAnswered
    from .nudge import nudge_if_waited
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "add_repo_answered",
                             "request_id": msg.get("request_id"), "agent_id": agent_id}
    request = asking.get(agent_id)
    if (request is None or request.tool != TOOL_ADD_REPO
            or not same_instant(_text(msg, "since"), request.arrived)):
        reply["error"] = "That request is no longer waiting."
        return reply
    grant = request.proposed.strip()
    if grant.endswith(":write") and msg.get("write") is not True:
        grant = grant.removesuffix(":write")
    grant_repo = getattr(bottles, "grant_repo", None)  # BottleService's
    result: dict[str, Any] = (
        {"error": "this host controller can't grant repos"} if grant_repo is None
        else await asyncio.to_thread(grant_repo, request.session, grant))
    if result.get("error"):
        reply["error"] = result["error"]
        return reply
    warnings: list[str] = []
    target = keep_target(request.session) if msg.get("keep") is True else None
    if target is not None:
        try:
            await asyncio.to_thread(keep_grant, target, grant, extends=result.get("extends", ""))
            reply["kept"] = target.label()
        except Exception as e:  # pylint: disable=broad-exception-caught  # granted anyway
            log.warn(f"remote: keeping {grant} failed: {type(e).__name__}: {e}")
            warnings.append(f"granted, but keeping it in {target.label()} failed")
    access = "write" if result.get("writable") else "read-only"
    notes = (f"granted {grant.removesuffix(':write')} ({access}); "
             f"use its normal URL: {result['url']}")
    try:
        await asyncio.to_thread(orchestrator(request.orchestrator).supervise_respond,
                                request.id, bottle_slug=request.bottle, decision="approve",
                                notes=notes)
        await asyncio.to_thread(nudge_if_waited, request, "approved", notes)
    except AlreadyAnswered:
        warnings.append("the request was answered elsewhere meanwhile")
    reply.update({"repo": result["repo"], "writable": result.get("writable", False),
                  "warnings": warnings})
    return reply


async def manifest(msg: Mapping[str, Any], running: Mapping[str, str],
                   _bottles: Any = None) -> dict[str, Any]:
    """A running agent's manifest for the console's Manifest tab
    (`manifest_view.agent_manifest`)."""
    # pylint: disable-next=import-outside-toplevel
    from .manifest_view import ManifestViewError, agent_manifest
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "manifest", "request_id": msg.get("request_id"),
                             "agent_id": agent_id}
    bottle = running.get(agent_id)
    if not bottle:
        reply["error"] = "that agent isn't running on this host"
        return reply
    try:
        reply.update(await asyncio.to_thread(agent_manifest, bottle))
    except ManifestViewError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: reading {bottle}'s manifest failed: {type(e).__name__}: {e}")
        reply["error"] = "reading the manifest failed; see the host controller's log"
    return reply


async def manifest_save(msg: Mapping[str, Any], running: Mapping[str, str],
                        bottles: Any = None) -> dict[str, Any]:
    """Save a running agent's manifest as its next version from the Manifest
    tab, and with `bottle` its changes into its bottle file too
    (`manifest_view.save_manifest`), if they're still the ones shown."""
    # pylint: disable-next=import-outside-toplevel
    from .manifest_view import ManifestViewError, remove_changes, save_as_bottle, save_manifest
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "manifest_saved", "request_id": msg.get("request_id"),
                             "agent_id": agent_id}
    bottle = running.get(agent_id)
    if not bottle:
        reply["error"] = "that agent isn't running on this host"
        return reply
    try:
        raw_keys = msg.get("keys")
        keys = [k for k in raw_keys if isinstance(k, str)] if isinstance(raw_keys, list) \
            else None
        digest = _text(msg, "digest")
        if msg.get("remove") is True:
            backend_for = getattr(bottles, "backend_for_session", None)  # BottleService's
            reply.update(await asyncio.to_thread(
                remove_changes, bottle, digest, keys or [],
                backend=backend_for(bottle) if backend_for else None))
        elif _text(msg, "new_bottle"):
            reply.update(await asyncio.to_thread(save_as_bottle, bottle, digest,
                                                 _text(msg, "new_bottle"),
                                                 extend=msg.get("extend") is True, keys=keys))
        else:
            reply.update(await asyncio.to_thread(save_manifest, bottle, digest,
                                                 bottle=_text(msg, "bottle"), keys=keys))
    except ManifestViewError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: keeping {bottle}'s changes failed: {type(e).__name__}: {e}")
        reply["error"] = "keeping the changes failed; see the host controller's log"
    return reply


# Console requests about one running agent, by message type.
QUERIES = {"refresh_credentials": refresh_credentials, "manifest": manifest,
           "manifest_save": manifest_save}

# Console answers to a card that carry more than allow/deny, by message type.
ANSWERS = {"forge_connect_answer": forge_connect_answer, "add_repo_answer": add_repo_answer}

__all__ = ["ANSWERS", "QUERIES", "add_repo_answer", "card", "forge_attention", "forge_connect",
           "forge_connect_answer", "manifest", "manifest_save", "refresh_credentials",
           "repo_attention"]
