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


async def workspace_file(msg: Mapping[str, Any], running: Mapping[str, str],
                         bottles: Any) -> dict[str, Any]:
    """An image file from a running agent's bottle, for the console to show
    (`workspace_files`)."""
    agent_id = str(msg.get("agent_id", ""))
    path = msg.get("path")
    bottle = running.get(agent_id)
    read = getattr(bottles, "read_workspace_image", None)  # BottleService's
    result: dict[str, object] = (
        {"error": "that agent isn't running on this host"} if not bottle
        else {"error": "that isn't a file path"} if not isinstance(path, str)
        else {"error": "this host controller can't read workspace files"} if read is None
        else await asyncio.to_thread(read, bottle, path))
    return {"type": "workspace_file", "request_id": msg.get("request_id"),
            "agent_id": agent_id, "path": path if isinstance(path, str) else "", **result}


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
    bottle = _text(msg, "bottle")  # "": the bottle named after the forge's kind
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


async def github_authorize_start(msg: Mapping[str, Any]) -> dict[str, Any]:
    """Start GitHub's device flow for the console (ADR 0012): the code the
    operator enters on GitHub. The host waits for them and connects;
    `github_status` follows it. The token never reaches the console."""
    # pylint: disable=import-outside-toplevel
    from ...forge_connect import ConnectError
    from ...github_connect import start_background
    from ...paths import bot_bottle_root
    reply: dict[str, Any] = {"type": "github_authorize", "request_id": msg.get("request_id")}
    try:
        reply.update(await asyncio.to_thread(
            start_background, bottle=_text(msg, "bottle"), cwd=Path.home(),
            host_bottles=bot_bottle_root() / "bottles"))
    except ConnectError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: starting GitHub's device flow failed: {type(e).__name__}")
        reply["error"] = "starting the authorization failed; see the host controller's log"
    return reply


async def github_status(msg: Mapping[str, Any]) -> dict[str, Any]:
    """The app this host's GitHub access goes through, a flow in progress, and
    who's connected with which installations."""
    # pylint: disable=import-outside-toplevel
    from ...github_connect import status
    from ...paths import bot_bottle_root
    reply: dict[str, Any] = {"type": "github_status", "request_id": msg.get("request_id")}
    try:
        reply.update(await asyncio.to_thread(status, bot_bottle_root() / "bottles"))
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
        log.warn(f"remote: reading GitHub's status failed: {type(e).__name__}")
        reply["error"] = "reading GitHub's status failed; see the host controller's log"
    return reply


def _connected_forges() -> dict[str, str]:
    """The hostnames of the forges this host's own bottles declare, and their
    kinds."""
    # pylint: disable=import-outside-toplevel
    from urllib.parse import urlparse
    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
    from ...paths import bot_bottle_root
    hosts: dict[str, str] = {}
    for path in sorted((bot_bottle_root() / "bottles").glob("*.md")):
        try:
            fm, _ = parse_frontmatter(path.read_text())
        except (OSError, YamlSubsetError):
            continue
        git = fm.get("git-gate")
        forges = git.get("forges") if isinstance(git, dict) else None
        for entry in forges.values() if isinstance(forges, dict) else ():
            if isinstance(entry, dict) and (host := urlparse(str(entry.get("url", ""))).hostname):
                hosts[host] = str(entry.get("kind") or "gitea")
    return hosts


def _connected_hosts() -> set[str]:
    """The hostnames of the forges this host's own bottles declare."""
    return set(_connected_forges())


def forge_attention(request: Any, kind: str = "") -> tuple[list[str], list[str]]:
    """What the phone shows for a `forge-connect` request, and its option. The
    URL is the agent's, so the card says when this host has never connected
    it: a token pasted for the wrong host is sent to that host. `kind` is the
    forge's (`forge_kinds.detect`); without one there's nothing to connect."""
    # pylint: disable=import-outside-toplevel
    from urllib.parse import urlparse
    from ...contract.forges import GITHUB_HOSTS, unsupported_forge
    from ...forge_kinds import forge_api, label
    url = request.proposed.strip()
    lines = [f"forge: {url}"]
    if request.justification.strip():
        lines.append(f"why: {request.justification.strip()}")
    host = urlparse(url).hostname or ""
    if not kind:
        return [*lines, unsupported_forge(host or url)], []
    if host not in _connected_hosts():
        lines.append("new to this host")
    if kind == "github" and host in GITHUB_HOSTS:
        # The bot-bottle GitHub App's device flow (ADR 0012), not a pasted token.
        from ...contrib.github.user_token import app_config
        app = app_config()
        return [*lines, f"kind: {label(kind)}", f"app: {app.slug}",
                f"install: {app.install_url}"], ["github_authorize"]
    api = forge_api(kind)
    lines += [f"kind: {label(kind)}", f"token page: {api.token_page(url)}",
              f"token help: {api.TOKEN_HELP}"]
    if kind == "gitea":  # what consoles before `token help` show
        lines.append(f"scopes: {', '.join(api.TOKEN_SCOPES)}")
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
    # Into the bottle named after the forge's kind: the card doesn't pick one.
    result = await forge_connect({"url": request.proposed, "token": _text(msg, "token"),
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
    reach, reauthorize = _app_reach_lines(found.forge, repo)
    lines += reach
    target = keep_target(request.session)
    options = [*(["github_authorize"] if reauthorize else []), "add_repo",
               *(["add_repo_write"] if writable else [])]
    if target is not None:
        lines.append(f"keep: in {target.label()}")
        options.append("add_repo_keep")
    return lines, options


def _app_reach_lines(forge: Any, repo: str) -> tuple[list[str], bool]:
    """For a repo on a GitHub forge reached through the bot-bottle app: the
    install link when the operator's token can't reach the repo yet, and
    whether the authorization itself is gone (the card then reconnects
    GitHub). ([], False) otherwise, or when GitHub can't be asked (granting
    then says why)."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import (
        ReauthorizeError, current_token, install_url, reachable,
    )
    from ...forge_kinds import ForgeApiError
    from ...paths import host_secrets_path
    from ...secret_source import FileSecrets
    if getattr(forge, "kind", "") != "github" or not getattr(forge, "app", ""):
        return [], False
    owner_repo = repo.split("/", 1)[1] if "/" in repo else repo
    try:
        token = current_token(forge.token_secret, FileSecrets(host_secrets_path()))
        if token is None:
            raise ReauthorizeError("no token")
        if reachable(token, owner_repo):
            return [], False
    except ReauthorizeError:
        return [f"app: {forge.app}", "reauthorize: GitHub's authorization for this host is "
                "gone; connect GitHub again, then grant"], True
    except ForgeApiError:
        return [], False
    owner = owner_repo.split("/", 1)[0]
    return [f"install: {install_url(forge.app)}",
            f"not installed: {forge.app} can't reach {owner_repo} yet; install it on {owner} "
            f"(or add the repo to its installation), then grant"], False


def card(request: Any, kind: str = "") -> tuple[list[str], list[str]] | None:
    """The phone card for a request that takes more than allow/deny, or None.
    `kind` is a `forge-connect`'s forge's (`vet`)."""
    # pylint: disable-next=import-outside-toplevel
    from ...contract.supervise import TOOL_ADD_REPO, TOOL_FORGE_CONNECT
    if request.tool == TOOL_FORGE_CONNECT:
        return forge_attention(request, kind)
    if request.tool == TOOL_ADD_REPO:
        return repo_attention(request)
    return None


def vet(request: Any) -> tuple[str, str]:
    """Whether an agent's `forge-connect` or `add-repo` can be met from a card:
    (why not, told to the agent; "" when it can) and a `forge-connect`'s forge
    kind. A forge bot-bottle can't connect, or a repo on a forge this host
    hasn't connected, can't. Asks an unknown forge what it is, briefly."""
    # pylint: disable=import-outside-toplevel
    from urllib.parse import urlparse
    from ...contract.forges import GITHUB_HOSTS, unsupported_forge
    from ...contract.supervise import TOOL_ADD_REPO, TOOL_FORGE_CONNECT
    from ...forge_kinds import detect
    from .repo_grants import find_forge
    if request.tool == TOOL_FORGE_CONNECT:
        url = request.proposed.strip()
        host = urlparse(url).hostname or ""
        connected = _connected_forges().get(host, "")
        if connected and host in GITHUB_HOSTS:
            return (f"{host} is already connected to this host; ask for its repos with "
                    f"add-repo"), connected
        kind = connected or detect(url)
        return ("" if kind else unsupported_forge(host or url)), kind
    if request.tool == TOOL_ADD_REPO:
        forge = request.proposed.strip().split("/", 1)[0]
        if find_forge(request.session, forge) is not None:
            return "", ""
        url = f"https://{forge.rsplit('@', 1)[-1]}"
        if not detect(url):
            return unsupported_forge(urlparse(url).hostname or forge), ""
        return (f"{forge} isn't connected to this host; ask for it with forge-connect "
                f"({url}) first, then add-repo again"), ""
    return "", ""


async def special_card(request: Any, orchestrator: Any) -> tuple[list[str], list[str]] | None:
    """`card`, after `vet`: a request no card could meet is refused for the
    operator, the agent told why, and the card is ([], []). None when the
    request takes only allow/deny. A refusal that fails shows the card, which
    the operator can deny."""
    # pylint: disable=import-outside-toplevel
    from ...contract.supervise import TOOL_ADD_REPO, TOOL_FORGE_CONNECT
    from ..client import AlreadyAnswered
    from .nudge import nudge_if_waited
    if request.tool not in (TOOL_FORGE_CONNECT, TOOL_ADD_REPO):
        return None
    refusal, kind = await asyncio.to_thread(vet, request)
    if refusal:
        try:
            await asyncio.to_thread(orchestrator(request.orchestrator).supervise_respond,
                                    request.id, bottle_slug=request.bottle, decision="reject",
                                    notes=refusal)
            await asyncio.to_thread(nudge_if_waited, request, "denied", refusal)
            log.info(f"remote: {request.session}: refused {request.tool}: {refusal}")
            return [], []
        except AlreadyAnswered:
            return [], []
        except Exception as e:  # pylint: disable=broad-exception-caught  # the card instead
            log.warn(f"remote: refusing {request.tool} failed: {type(e).__name__}: {e}")
    return await asyncio.to_thread(card, request, kind)


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


async def manifest_transfer(msg: Mapping[str, Any], running: Mapping[str, str],
                            bottles: Any = None) -> dict[str, Any]:
    """Copy or move picked elements of a running agent's manifest into another
    bottle (`manifest_elements.transfer`), or with `delete`, delete them
    (`manifest_elements.delete`)."""
    # pylint: disable-next=import-outside-toplevel
    from .manifest_elements import ManifestViewError, delete, transfer
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "manifest_transferred",
                             "request_id": msg.get("request_id"), "agent_id": agent_id}
    bottle = running.get(agent_id)
    if not bottle:
        reply["error"] = "that agent isn't running on this host"
        return reply
    raw = msg.get("keys")
    keys = [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else []
    try:
        if msg.get("delete") is True:
            backend_for = getattr(bottles, "backend_for_session", None)  # BottleService's
            reply.update(await asyncio.to_thread(
                delete, bottle, keys, backend=backend_for(bottle) if backend_for else None))
        else:
            reply.update(await asyncio.to_thread(transfer, bottle, keys, _text(msg, "target"),
                                                 move=msg.get("move") is True))
    except ManifestViewError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: copying {bottle}'s elements failed: {type(e).__name__}: {e}")
        reply["error"] = "copying failed; see the host controller's log"
    return reply


async def bottle_chain(msg: Mapping[str, Any], running: Mapping[str, str],
                       _bottles: Any = None) -> dict[str, Any]:
    """A running agent's bottle files as data, for the console's editable
    Bottle tab (`bottle_chain.chain`)."""
    # pylint: disable=import-outside-toplevel
    from .bottle_chain import chain
    from .manifest_view import ManifestViewError, _live_policy
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "bottle_chain", "request_id": msg.get("request_id"),
                             "agent_id": agent_id}
    bottle = running.get(agent_id)
    if not bottle:
        reply["error"] = "that agent isn't running on this host"
        return reply
    try:
        policy = await asyncio.to_thread(_live_policy, bottle)
        reply.update(await asyncio.to_thread(chain, bottle, policy))
    except ManifestViewError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: reading {bottle}'s bottles failed: {type(e).__name__}: {e}")
        reply["error"] = "reading the bottles failed; see the host controller's log"
    return reply


async def bottle_write(msg: Mapping[str, Any], running: Mapping[str, str],
                       _bottles: Any = None) -> dict[str, Any]:
    """Write one host bottle of a running agent's chain from the console, as
    data or raw text, if it's still what was shown (`bottle_chain.write`).
    `stale` says it changed meanwhile."""
    # pylint: disable-next=import-outside-toplevel
    from .bottle_chain import StaleBottleError, write
    from .manifest_view import ManifestViewError
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "bottle_written", "request_id": msg.get("request_id"),
                             "agent_id": agent_id}
    bottle = running.get(agent_id)
    if not bottle:
        reply["error"] = "that agent isn't running on this host"
        return reply
    try:
        reply.update(await asyncio.to_thread(
            write, bottle, _text(msg, "bottle"), _text(msg, "digest"),
            frontmatter=msg.get("frontmatter"), body=msg.get("body"),
            content=msg.get("content")))
    except StaleBottleError as e:
        reply.update({"error": str(e), "stale": True})
    except ManifestViewError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: writing {bottle}'s bottle failed: {type(e).__name__}: {e}")
        reply["error"] = "writing the bottle failed; see the host controller's log"
    return reply


async def list_secrets(msg: Mapping[str, Any], _running: Mapping[str, str],
                       _bottles: Any = None) -> dict[str, Any]:
    """The names in the host secrets file, for the console's Credentials tab.
    Values are never returned."""
    # pylint: disable=import-outside-toplevel
    from ...paths import host_secrets_path
    from ...secret_source import FileSecrets
    reply: dict[str, Any] = {"type": "secrets_listed", "request_id": msg.get("request_id")}
    try:
        reply["names"] = [name for name, _ in FileSecrets(host_secrets_path()).items()]
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"remote: listing secrets failed: {type(e).__name__}: {e}")
        reply["error"] = "listing secrets failed; see the host controller's log"
    return reply


async def set_secret(msg: Mapping[str, Any], _running: Mapping[str, str],
                     _bottles: Any = None) -> dict[str, Any]:
    """Set a value in the host secrets file from the console. The value is
    never echoed in the reply, a log line, or an error."""
    # pylint: disable=import-outside-toplevel
    from ...errors import SecretsFileError
    from ...paths import host_secrets_path
    from ...secret_source import write_secret
    name = _text(msg, "name")
    reply: dict[str, Any] = {"type": "secret_set", "request_id": msg.get("request_id"),
                             "name": name}
    value = _text(msg, "value")
    try:
        write_secret(host_secrets_path(), name, value)
        log.info(f"remote: set secret {name!r} from the console")
    except SecretsFileError as e:
        reply["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"remote: setting secret {name!r} failed: {type(e).__name__}: {e}")
        reply["error"] = "setting the secret failed; see the host controller's log"
    return reply


async def library(msg: Mapping[str, Any], _running: Mapping[str, str],
                  _bottles: Any = None) -> dict[str, Any]:
    """The host's agents and bottles, with their text, for the console's
    Library (`library.host_library`)."""
    # pylint: disable-next=import-outside-toplevel
    from .library import host_library
    reply: dict[str, Any] = {"type": "library", "request_id": msg.get("request_id")}
    try:
        reply.update(await asyncio.to_thread(host_library))
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: listing the library failed: {type(e).__name__}: {e}")
        reply["error"] = "listing the host's agents and bottles failed; see its log"
    return reply


async def resolve(msg: Mapping[str, Any], _running: Mapping[str, str],
                  _bottles: Any = None) -> dict[str, Any]:
    """What launching the named agent in the named bottles would run with,
    for the console's launch page (`library.resolved_manifest`). It names
    things as a launch does, so it's checked as one is."""
    # pylint: disable=import-outside-toplevel
    from .library import resolved_manifest
    from .remote_requests import MAX_BOTTLES, _NAME
    reply: dict[str, Any] = {"type": "resolved", "request_id": msg.get("request_id")}
    agent, bottles = msg.get("agent"), msg.get("bottles", [])
    if not isinstance(agent, str) or not _NAME.match(agent):
        reply["error"] = "resolve needs an agent's name"
        return reply
    if (not isinstance(bottles, list) or len(bottles) > MAX_BOTTLES
            or not all(isinstance(b, str) and _NAME.match(b) for b in bottles)):
        reply["error"] = f"bottles must be a list of at most {MAX_BOTTLES} bottle names"
        return reply
    try:
        reply.update(await asyncio.to_thread(resolved_manifest, agent, bottles))
    except Exception as e:  # pylint: disable=broad-exception-caught  # reported, not raised
        log.warn(f"remote: resolving {agent}'s manifest failed: {type(e).__name__}: {e}")
        reply["error"] = "resolving the manifest failed; see the host controller's log"
    return reply


# Console requests (most about one running agent), by message type.
QUERIES = {"refresh_credentials": refresh_credentials, "manifest": manifest,
           "bottle_chain": bottle_chain, "bottle_write": bottle_write,
           "manifest_save": manifest_save, "manifest_transfer": manifest_transfer,
           "library": library, "resolve": resolve,
           "list_secrets": list_secrets, "set_secret": set_secret,
           "workspace_file": workspace_file}

# Console answers to a card that carry more than allow/deny, by message type.
ANSWERS = {"forge_connect_answer": forge_connect_answer, "add_repo_answer": add_repo_answer}
# Requests about this host rather than one agent.
HOST_REQUESTS = {"forge_connect": forge_connect, "github_authorize_start": github_authorize_start,
                 "github_status": github_status}

__all__ = ["ANSWERS", "HOST_REQUESTS", "QUERIES", "add_repo_answer", "bottle_chain",
           "bottle_write", "card", "forge_attention", "forge_connect", "forge_connect_answer",
           "github_authorize_start", "github_status", "library", "list_secrets", "manifest",
           "manifest_save", "manifest_transfer", "refresh_credentials", "repo_attention",
           "resolve", "set_secret", "workspace_file"]
