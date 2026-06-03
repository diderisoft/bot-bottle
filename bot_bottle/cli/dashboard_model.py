"""dashboard_model: state/model layer for the dashboard TUI.

Data structures, discovery queries, pure state helpers, and derived
values extracted from dashboard.py so they can be tested in isolation
and navigated without wading through curses rendering code.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from .. import supervise as _supervise
from ..agent_provider import runtime_for
from ..backend import ActiveAgent, enumerate_active_agents
from ..backend.docker.capability_apply import CapabilityApplyError
from ..backend.docker.egress_apply import EgressApplyError
from ..backend.docker.pipelock_apply import PipelockApplyError
from ..manifest import Manifest
from ..supervise import (
    TOOL_CAPABILITY_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    Proposal,
    list_pending_proposals,
)


# --- Constants ---------------------------------------------------------------


_REFRESH_INTERVAL_MS = 1000

_NEW_PROPOSAL_HIGHLIGHT_SEC = 5.0

PANE_PROPOSALS = "proposals"
PANE_AGENTS = "agents"


# --- Data structures ---------------------------------------------------------


@dataclass(frozen=True)
class QueuedProposal:
    """A pending proposal plus the queue dir it was found in."""

    proposal: Proposal
    queue_dir: Path


ApplyError = (EgressApplyError, PipelockApplyError, CapabilityApplyError)


# --- Discovery ---------------------------------------------------------------


def discover_active_agents() -> list[ActiveAgent]:
    """All currently-running agents across every backend with
    their metadata + service set. Returns [] when neither
    backend is reachable. Backed by the shared
    `enumerate_active_agents` helper so the CLI's
    `./cli.py list active` and this dashboard show the same data."""
    return enumerate_active_agents()


def discover_pending() -> list[QueuedProposal]:
    """Walk ~/.bot-bottle/queue/* and collect pending proposals
    from every bottle's queue. Sorted by arrival time across the
    union — the operator works the global FIFO."""
    queue_root = _supervise.bot_bottle_root() / "queue"
    if not queue_root.is_dir():
        return []
    out: list[QueuedProposal] = []
    for slug_dir in sorted(queue_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for proposal in list_pending_proposals(slug_dir):
            out.append(QueuedProposal(proposal=proposal, queue_dir=slug_dir))
    out.sort(key=lambda q: q.proposal.arrival_timestamp)
    return out


# --- Derived values ----------------------------------------------------------


def _approval_status(qp: QueuedProposal, verb: str) -> str:
    """Status-line text after a successful approval. For capability-
    block, append the `resume <identity>` hint so the operator can
    bring the rebuilt bottle back up with one copy-paste."""
    base = f"{verb} {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
    if qp.proposal.tool == TOOL_CAPABILITY_BLOCK:
        return f"{base}; resume: ./cli.py resume {qp.proposal.bottle_slug}"
    return base


def _is_recent(
    proposal_id: str,
    first_seen: dict[str, float] | None,
    now: float | None,
) -> bool:
    """True if `proposal_id` was first seen within the highlight
    window. Both `first_seen` and `now` may be None (rendered as
    not-recent) so the helper is safe in cold-start paths."""
    if first_seen is None or now is None:
        return False
    started = first_seen.get(proposal_id)
    if started is None:
        return False
    return (now - started) < _NEW_PROPOSAL_HIGHLIGHT_SEC


def _selection_status(
    focus: str, agents: list[ActiveAgent], selected_agent: int,
) -> str:
    """Status-line text for the idle state. Surfaces the agents-
    pane selection so the operator can tell what an agent-scoped
    edit verb would target."""
    if focus != PANE_AGENTS:
        return ""
    if not agents:
        return "[no active agents]"
    if 0 <= selected_agent < len(agents):
        return f"[selected: {agents[selected_agent].slug}]"
    return "[no agent selected]"


def _selected_agent(
    focus: str, agents: list[ActiveAgent], selected_agent: int,
) -> ActiveAgent | None:
    """The selected agent to scope `e` / `p` to, or None if no
    selection is valid (proposals pane focused, no active agents,
    or selection out of bounds)."""
    if focus != PANE_AGENTS:
        return None
    if not agents:
        return None
    if 0 <= selected_agent < len(agents):
        return agents[selected_agent]
    return None


# --- Picker helpers ----------------------------------------------------------


def _filter_agents(query: str, names: list[str]) -> list[str]:
    """Case-insensitive substring filter for the picker. Pure
    function — no curses, easy to unit-test."""
    if not query:
        return list(names)
    q = query.lower()
    return [n for n in names if q in n.lower()]


def _running_counts(
    bottles: dict, agents_now: list[ActiveAgent],
) -> dict[str, int]:
    """Per-agent running count: dashboard-owned + externally-
    discovered, summed by agent_name. The picker shows this so the
    operator knows whether picking an agent starts a fresh bottle
    or a Nth one."""
    counts: dict[str, int] = {}
    for a in agents_now:
        counts[a.agent_name] = counts.get(a.agent_name, 0) + 1
    return counts


# --- Agent-row rendering helpers ---------------------------------------------


def _format_agent_row(a: ActiveAgent, maxw: int) -> str:
    """One-line agent row: `  [<backend>] <slug> <agent_name> started
    <HH:MM:SS> [<sidecars>]`. The `agent` service is filtered out of
    the displayed list — it's always present for an active bottle,
    so listing it carries no information; the sidecars are the
    differentiator.

    The `[docker]` / `[smolmachines]` prefix lets the operator tell
    which backend a bottle came from (issue #77). Truncated to
    `maxw` because the renderer's addnstr only enforces width if
    we hand it a properly-sized string."""
    started = (
        a.started_at.split("T", 1)[1][:8]
        if "T" in a.started_at else (a.started_at or "?")
    )
    sidecars = tuple(s for s in a.services if s != "agent")
    services = ",".join(sidecars) if sidecars else "(starting)"
    backend_tag = f"[{a.backend_name}]" if a.backend_name else ""
    line = (
        f"  {backend_tag} {a.slug}  {a.agent_name}  "
        f"started {started}  [{services}]"
    )
    if len(line) > maxw:
        return line[: max(0, maxw - 1)] + "…"
    return line


# --- Detail-view helpers -----------------------------------------------------


def _detail_lines(
    qp: QueuedProposal,
    *,
    green_attr: int = 0,
) -> list[tuple[str, int]]:
    """Return the detail-view body as (text, curses-attr) tuples.
    Most lines are plain (attr=0); pipelock-block proposals append
    a green "→ would allow host: ..." line so the operator sees at
    a glance which hostname will land in pipelock's allowlist if
    they hit approve. The URL itself is shown above for context."""
    p = qp.proposal
    out: list[tuple[str, int]] = [
        (f"bottle: {p.bottle_slug}", 0),
        (f"tool: {p.tool}", 0),
        (f"id: {p.id}", 0),
        (f"arrived: {p.arrival_timestamp}", 0),
        (f"queue: {qp.queue_dir}", 0),
        ("", 0),
        ("justification:", 0),
    ]
    out.extend(("  " + line, 0) for line in p.justification.splitlines() or [""])
    out.extend([
        ("", 0),
        (_proposed_payload_label(p.tool) + ":", 0),
    ])
    out.extend((line, 0) for line in p.proposed_file.splitlines() or [""])
    if p.tool == TOOL_PIPELOCK_BLOCK:
        host = _failed_url_host(p.proposed_file)
        if host:
            out.append(("", 0))
            out.append((host, green_attr))
    return out


def _failed_url_host(url: str) -> str:
    """Best-effort hostname extraction from a pipelock-block proposal's
    failed_url payload. Returns empty string on unparseable input —
    callers handle empty as "nothing to highlight"."""
    import urllib.parse
    try:
        return urllib.parse.urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""


def _proposed_payload_label(tool: str) -> str:
    """The detail-view section heading for the proposal's payload —
    `proposed_file` is what the dataclass calls it, but for
    pipelock-block the payload is a single URL not a file. Render
    the label per tool so the operator's eye matches."""
    if tool == TOOL_PIPELOCK_BLOCK:
        return "failed URL"
    return "proposed file"


def _suffix_for_tool(tool: str) -> str:
    if tool == TOOL_CAPABILITY_BLOCK:
        return ".dockerfile"
    return ".txt"


# --- Bottle/agent resolution -------------------------------------------------


def _bottle_for_slug(
    slug: str,
    bottles: dict,
    manifest: Manifest | None,
) -> tuple["object", str]:
    """Return `(bottle_handle, prompt_path_hint)` for a re-attach.
    If the slug is in `bottles` (dashboard-owned), return the stored
    handle directly. Otherwise synthesize a bottle from the persisted
    metadata. The backend field in metadata (PRD 0040) selects Docker
    or smolmachines; unknown or missing metadata defaults to Docker.

    Returns the empty string for prompt_path_hint when we omit the
    flag — the caller passes None to DockerBottle in that case."""
    from ..backend.docker.bottle import DockerBottle
    from ..backend.docker.bottle_state import read_metadata
    from ..backend.smolmachines.bottle import SmolmachinesBottle
    if slug in bottles:
        _cm, bottle, _identity = bottles[slug]
        return bottle, ""
    instance_name = f"bot-bottle-{slug}"
    prompt_path: str | None = None
    metadata = read_metadata(slug)
    if metadata is not None and manifest is not None:
        agent = manifest.agents.get(metadata.agent_name)
        if agent is not None and agent.prompt:
            container_home = os.environ.get(
                "BOT_BOTTLE_CONTAINER_HOME", "/home/node",
            )
            prompt_path = f"{container_home}/.bot-bottle-prompt.txt"
    backend = metadata.backend if metadata is not None else ""
    if backend == "smolmachines":
        synth: object = SmolmachinesBottle(
            instance_name,
            prompt_path=prompt_path,
        )
    else:
        synth = DockerBottle(
            container=instance_name,
            teardown=lambda: None,
            prompt_path_in_container=prompt_path,
        )
    return synth, (prompt_path or "")


def _pick_next_after_stop(
    agents_before: list[ActiveAgent],
    selected_index: int,
    stopped_slug: str,
) -> tuple[int, ActiveAgent] | None:
    """After stopping `stopped_slug` from the agents list, choose
    the agent that should take focus next. The agent below the
    stopped row (which slides up to fill its index) is the
    natural pick; if the stopped agent was last, the row above
    instead. Returns (new_index, agent) or None if no agents
    remain. Pure — easy to unit-test."""
    new_agents = [a for a in agents_before if a.slug != stopped_slug]
    if not new_agents:
        return None
    new_index = min(max(selected_index, 0), len(new_agents) - 1)
    return new_index, new_agents[new_index]


# --- tmux argv builders ------------------------------------------------------


def _in_tmux() -> bool:
    """True when the dashboard is running inside a tmux session.
    Tmux sets `$TMUX` to the path of its server socket."""
    return bool(os.environ.get("TMUX"))


def _agent_runtime_args(
    *, resume: bool, remote_control: bool = False, agent_provider_template: str = "claude",
) -> list[str]:
    """The argv the dashboard hands to `bottle.agent_argv`
    on every attach — matches what `attach_agent` builds for the
    foreground handoff so both surfaces produce the same claude
    invocation."""
    runtime = runtime_for(agent_provider_template)
    args = list(runtime.bypass_args)
    if remote_control:
        args.extend(runtime.remote_control_args)
    if resume:
        args.extend(runtime.resume_args)
    return args


def _build_resume_argv_with_fallback(
    bottle, *, remote_control: bool = False, agent_provider_template: str = "claude",
) -> list[str]:
    """Build a backend-exec argv that runs `claude --continue` and
    falls back to plain `claude` if no prior session exists.

    `--continue` exits non-zero when an agent has been spun up
    but never typed at — there's no transcript to resume. The
    shell-level `||` wrapper makes that case start a fresh
    session instead of crashing the pane. The trade-off: we
    invoke `sh -c` inside the bottle, so the command is two
    `claude` invocations behind a tiny shell rather than one
    direct exec. Acceptable; the shell adds microseconds and
    the fallback only kicks in when --continue would have
    failed anyway.

    Works across backends because `bottle.agent_argv` always
    surfaces the `claude` token preceded by the backend's exec
    framing (docker: `docker exec -it <c>`; smolmachines:
    `smolvm machine exec --name <m> -- runuser -u node --`).
    Splitting at `claude` keeps the framing as the prefix and
    wraps just the agent tail in `sh -c`."""
    if agent_provider_template != "claude":
        return bottle.agent_argv(
            _agent_runtime_args(
                resume=True,
                remote_control=remote_control,
                agent_provider_template=agent_provider_template,
            )
        )
    base_args = _agent_runtime_args(
        resume=False,
        remote_control=remote_control,
        agent_provider_template=agent_provider_template,
    )
    base_exec = bottle.agent_argv(base_args)
    # Split exec-framing prefix from the agent-and-args tail so
    # we can compose `<claude…> --continue || <claude…>` inside
    # `sh -c`. The provider command token is the marker.
    command = getattr(bottle, "agent_command", runtime_for(agent_provider_template).command)
    agent_idx = base_exec.index(command)
    prefix = base_exec[:agent_idx]
    agent_cmd = " ".join(shlex.quote(a) for a in base_exec[agent_idx:])
    resume_args = " ".join(
        shlex.quote(a) for a in runtime_for(agent_provider_template).resume_args
    )
    return [
        *prefix,
        "sh", "-c",
        f"{agent_cmd} {resume_args} || {agent_cmd}",
    ]


def _build_split_pane_argv(agent_argv: list[str]) -> list[str]:
    """Pure helper: wrap a backend-exec argv with `tmux split-window
    -h -P -F '#{pane_id}'`. The `-P -F` combo tells tmux to print
    the new pane's id on stdout so we can track it for later
    `respawn-pane` calls."""
    return [
        "tmux", "split-window", "-h",
        "-P", "-F", "#{pane_id}",
        *agent_argv,
    ]


def _build_respawn_pane_argv(pane_id: str, agent_argv: list[str]) -> list[str]:
    """Pure helper: wrap a backend-exec argv with `tmux respawn-pane
    -k -t <pane_id>`. `-k` kills the existing process in the pane
    before respawning."""
    return ["tmux", "respawn-pane", "-k", "-t", pane_id, *agent_argv]
