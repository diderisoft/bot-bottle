"""Console card and answer for `launch-bottle` (PRD 0106): launch a child
bottle at an agent's request, trimmed to within the parent's access."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ...lib import log


# --- Proposal parsing ---


def parse_launch_proposal(proposed_json: str) -> dict[str, Any]:
    """Parse the proposed_file JSON blob; raises ValueError on bad input."""
    try:
        d = json.loads(proposed_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"launch proposal is not valid JSON: {e}") from e
    if not isinstance(d, dict):
        raise ValueError("launch proposal must be a JSON object")
    agent = d.get("agent", "")
    if not isinstance(agent, str) or not agent:
        raise ValueError("launch proposal: 'agent' must be a non-empty string")
    bottles = d.get("bottles", [])
    if not isinstance(bottles, list):
        raise ValueError("launch proposal: 'bottles' must be a list")
    prompt = d.get("prompt", "")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("launch proposal: 'prompt' must be a non-empty string")
    label = d.get("label", "")
    if not isinstance(label, str):
        raise ValueError("launch proposal: 'label' must be a string")
    return {"agent": agent, "bottles": [str(b) for b in bottles],
            "prompt": prompt, "label": label}


# --- Access comparison ---


def compare_access(
    child_elements: list[dict[str, Any]],
    parent_elements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split `child_elements` into (within, extra) based on the parent's
    live elements. `resources:` elements are excluded from comparison and
    always returned as within (they don't represent access)."""
    parent_keys = {e["key"] for e in parent_elements}
    within: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    for elem in child_elements:
        if elem["key"].startswith("resources:"):
            within.append(elem)
        elif elem["key"] in parent_keys:
            within.append(elem)
        else:
            extra.append(elem)
    return within, extra


# --- Trimmed bottle builder ---


def trimmed_bottle_text(
    child_names: "tuple[str, ...] | list[str]",
    dirs: "list[tuple[Any, bool]]",
    within_elements: list[dict[str, Any]],
) -> str:
    """Build a standalone run-bottle text that contains only the within-parent
    elements from `child_names`'s chain (re-read from their source files).
    Resources are included as declared in the chain files."""
    # pylint: disable=import-outside-toplevel
    from ...contract.yaml_subset import serialize_yaml_subset
    from .manifest_elements import _chain_files, _file_elements, _set
    within_keys = {e["key"] for e in within_elements}
    fm: dict[str, Any] = {}
    seen: set[str] = set()
    chain = _chain_files(child_names, dirs)
    for f in chain:
        try:
            from ...contract.yaml_subset import parse_frontmatter
            file_fm = parse_frontmatter(f["path"].read_text(encoding="utf-8"))[0]
        except (OSError, ValueError):
            continue
        for key, _, _, raw_value in _file_elements(dict(file_fm)):
            if key not in seen and key in within_keys:
                seen.add(key)
                _set(fm, key, raw_value)
        # Resources are always included, using the nearest declaration.
        resources = file_fm.get("resources")
        if isinstance(resources, dict) and "resources" not in fm:
            fm["resources"] = dict(resources)
    return f"---\n{serialize_yaml_subset(fm)}---\n"


# --- Vet (auto-reject before card) ---


def vet_launch_bottle(request: Any, bottles: Any) -> str:
    """Whether `request` can be met: empty when it can, else why not (shown
    to the agent). Checks depth, children cap, agent/bottle names, and forge
    connections."""
    # pylint: disable=import-outside-toplevel
    from ...manifest import ManifestIndex
    from ...manifest.util import ManifestError
    from .children import DEFAULT_MAX_CHILDREN, is_launch_bottle_child, running_children_of
    try:
        parsed = parse_launch_proposal(request.proposed)
    except ValueError as e:
        return str(e)

    # Depth check: a child can't launch bottles of its own (depth 1).
    if is_launch_bottle_child(request.session):
        return ("this bottle was itself launched by another bottle; "
                "nested launch-bottle calls are not allowed")

    # Children cap.
    count = running_children_of(request.session, bottles)
    if count >= DEFAULT_MAX_CHILDREN:
        return (f"this bottle already has {count} running "
                f"{'child' if count == 1 else 'children'}; "
                f"the cap is {DEFAULT_MAX_CHILDREN}")

    # Agent and bottle name validation (like a console launch).
    try:
        index = ManifestIndex.resolve(str(__import__("pathlib").Path.home()), missing_ok=True)
    except ManifestError as e:
        return f"can't read the manifest: {e}"
    try:
        index.require_agent(parsed["agent"])
    except ManifestError as e:
        return str(e)

    child_names: tuple[str, ...] = tuple(parsed["bottles"])
    dirs = index.bottle_dirs()
    if child_names:
        from ...manifest.loader import load_bottle_chain
        for bname in child_names:
            try:
                load_bottle_chain(bname, dirs)
            except ManifestError:
                available = ", ".join(index.all_bottle_names) or "(none)"
                return (f"bottle '{bname}' not defined. Available: {available}")

    # Forge connection check: each repo grant's forge must be connected.
    from ...forge_connections import ForgeConnectionError, connections as forge_connections
    from .manifest_elements import _chain_files, _file_elements
    from ...contract.yaml_subset import parse_frontmatter
    try:
        conns = {c.host.split(":", 1)[0] for c in forge_connections().values()}
    except ForgeConnectionError:
        conns = set()
    chain = _chain_files(child_names or (), dirs)
    for f in chain:
        try:
            file_fm = parse_frontmatter(f["path"].read_text(encoding="utf-8"))[0]
        except (OSError, ValueError):
            continue
        for key, _, detail, _ in _file_elements(dict(file_fm)):
            if not key.startswith("repo:"):
                continue
            # detail is the grant string for string grants (forge/owner/repo[:write])
            if "/" in detail:
                forge_host = detail.removesuffix(":write").split("/", 1)[0]
                if forge_host and forge_host not in conns:
                    return (f"forge {forge_host!r} isn't connected to this host; "
                            f"run `bot-bottle forge connect https://{forge_host}` first")
    return ""


# --- Card ---


def launch_bottle_attention(
    request: Any,
    parent_elements: list[dict[str, Any]],
    child_elements: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Card lines and options for a `launch-bottle` request."""
    try:
        parsed = parse_launch_proposal(request.proposed)
    except ValueError as e:
        return [f"launch-bottle: bad proposal: {e}"], []
    _, extra = compare_access(child_elements, parent_elements)
    agent_name = parsed["agent"]
    child_names = parsed["bottles"]
    prompt = parsed["prompt"]
    label = parsed["label"]
    lines = [f"agent: {agent_name}"]
    if child_names:
        lines.append(f"bottles: {', '.join(child_names)}")
    lines.append(f"prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}")
    if label:
        lines.append(f"label: {label}")
    if request.justification.strip():
        lines.append(f"why: {request.justification.strip()}")
    if extra:
        removed: list[str] = []
        for e in extra:
            kind = e["key"].split(":", 1)[0]
            if kind == "egress":
                removed.append(f"egress {e['name']} (not in parent)")
            elif kind == "repo":
                removed.append(f"repo {e['name']} (not in parent)")
            elif kind == "env":
                removed.append(f"env {e['name']} (not in parent)")
            elif kind == "forge":
                removed.append(f"forge {e['name']} (not in parent)")
            else:
                removed.append(f"{e['key']} (not in parent)")
        lines.append(f"removed ({len(removed)}): {', '.join(removed[:5])}"
                     + (f" (+{len(removed)-5} more)" if len(removed) > 5 else ""))
    return lines, ["launch_bottle"]


# --- Answer ---


async def launch_bottle_answer(
    msg: Any,
    asking: Any,
    orchestrator: Any,
    bottles: Any,
) -> dict[str, Any]:
    """The phone's answer to a `launch-bottle` request: trim access, launch
    the child, record lineage, then approve with the child's agent_id."""
    # pylint: disable=import-outside-toplevel
    from ...bottle_state import record_parent
    from ...contract.auth import ROLE_CLI
    from ...contract.hostctl import LaunchRequest
    from ...contract.supervise import TOOL_LAUNCH_BOTTLE
    from ...manifest import ManifestIndex
    from ...run_bottle import RUN_BOTTLE
    from ...util import same_instant
    from ..client import AlreadyAnswered
    from .manifest_elements import bottle_elements, elements
    from .manifest_view import _live_policy
    from .nudge import nudge_if_waited

    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "launch_bottle_answered",
                             "request_id": msg.get("request_id"), "agent_id": agent_id}
    request = asking.get(agent_id)
    if (request is None or request.tool != TOOL_LAUNCH_BOTTLE
            or not same_instant(str(msg.get("since", "")), request.arrived)):
        reply["error"] = "That request is no longer waiting."
        return reply

    try:
        parsed = parse_launch_proposal(request.proposed)
    except ValueError as e:
        reply["error"] = str(e)
        return reply

    # Build the trimmed bottle from the child's static elements, filtered to
    # those within the parent's live access.
    try:
        index = ManifestIndex.resolve(str(__import__("pathlib").Path.home()), missing_ok=True)
        dirs = index.bottle_dirs()
        child_names = tuple(parsed["bottles"])
        parent_policy = await asyncio.to_thread(_live_policy, request.session)
        parent_elems = await asyncio.to_thread(elements, request.session, parent_policy)
        child_elems = bottle_elements(child_names, dirs)
        within, extra = compare_access(child_elems, parent_elems)
        trimmed = trimmed_bottle_text(child_names, dirs, within)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"remote: building trimmed bottle failed: {type(e).__name__}: {e}")
        reply["error"] = "building the trimmed bottle failed; see the host controller's log"
        return reply

    launch_req = LaunchRequest(
        agent=parsed["agent"],
        bottles=(*child_names, RUN_BOTTLE),
        prompt=parsed["prompt"],
        label=parsed["label"] or "",
        headless=True,
        assume_yes=True,
        run_bottle=trimmed,
    )

    launch_fn = getattr(bottles, "launch", None)
    if launch_fn is None:
        reply["error"] = "this host controller can't launch bottles"
        return reply

    try:
        result: dict[str, Any] = await asyncio.to_thread(launch_fn, launch_req, ROLE_CLI)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"remote: launch-bottle failed: {type(e).__name__}: {e}")
        reply["error"] = f"launching the child bottle failed: {e}"
        return reply

    child_identity = str(result.get("bottle", ""))
    if child_identity:
        await asyncio.to_thread(record_parent, child_identity, request.session)

    removed_count = len(extra)
    notes_parts = [f"launched {parsed['agent']}"]
    if child_identity:
        notes_parts.append(f"identity={child_identity}")
    if removed_count:
        notes_parts.append(f"{removed_count} extra element(s) removed")
    notes = "; ".join(notes_parts)

    try:
        await asyncio.to_thread(orchestrator(request.orchestrator).supervise_respond,
                                request.id, bottle_slug=request.bottle, decision="approve",
                                notes=notes)
        await asyncio.to_thread(nudge_if_waited, request, "approved", notes)
    except AlreadyAnswered:
        reply["warnings"] = ["the request was answered elsewhere meanwhile"]

    reply.update({"child_identity": child_identity, "agent": parsed["agent"],
                  "label": parsed["label"], "removed": removed_count})
    return reply


__all__ = [
    "compare_access",
    "launch_bottle_answer",
    "launch_bottle_attention",
    "parse_launch_proposal",
    "trimmed_bottle_text",
    "vet_launch_bottle",
]
