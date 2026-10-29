"""Remote access: the host end of the console link over iroh (ADR 0007).

With `bot-bottle pair` (or `pair on`), the host controller dials its console on ALPN
`bb-console/1`. Dial-out only; a console whose node id isn't the one pinned at
login is refused. The console knows the host by its node id.

The control stream (JSON lines):

- `list_local` -> `local_definitions`: the agents under `~/.bot-bottle/`.
- `launch` -> runs a local agent by name as role `remote`, with optional
  `prompt`, `bottles` and `label`; replies `agent_started` (bottle,
  `activation`, `writable`) or `agent_errored`. Agent and bottle content from
  the console is refused.
- `stop` -> tears the bottle down; `agent_stopped` follows, also for agents
  that exit on their own.
- `resume` (`agent_id`, `bottle`) -> relaunches a stopped bottle this host ran,
  under its own identity and state, as `bot-bottle resume` does
  (`remote_requests`); `agent_started` or `agent_errored` as for `launch`.
- `hello` opens the stream. On every (re)connect the host reports agents
  that exited, reopens running ones' relays, and sends `agents_running`.
- `agent_present` (from the host): a running session the console didn't
  launch (`bot-bottle`, `start`), with a host-made `agent_id`, its `bottle`,
  `activation`, `label`, `definition`, `writable` and `origin`. Sent on every
  (re)connect and when one starts. From then on it's one of the link's
  agents like any other: attention, approvals, the terminal relay, history,
  the audit query and `stop` all work on it.
- `attention` (from the host): a pending supervise request for one of the
  link's agents, with `since`, `message` and `options`; `activity` once
  nothing waits.
- `approval` -> `allow`, `allow_project` or `deny`, echoing `since` so a
  stale click can't answer a newer request.
- Terminals relay on uni streams (`console_terminal`); `input`, `resize` and
  `history` (`lines` -> `text`, `size`, `limit`) act on them.
- `audit_query` -> `audit_events` for the link's agents' bottles.
- `refresh_credentials` -> `credentials_refreshed` (`refreshed`, `expires_at`,
  or `error`): re-reads the agent's forwarded host login now
  (`credential_refresh`).
- `manifest` (`agent_id`) -> `manifest`: its agent and bottle files and what
  they resolve to (`manifest_view`).
- `forge_connect` (`url`, `token`, `bottle`, `name`, `ssh`) -> `forge_connected`
  (`key`, `account`, `admin_repos`, `repos`, `host_key`, `warnings`, or
  `error`): connects a forge into a host bottle (`remote_actions`). The token
  goes only to the forge and the secrets file.
- `snapshot` -> `snapshot_taken` (`snapshot`, or `error`): the agent's
  workspace patch and conversation position, kept on the host (`snapshots`).
- `transcript` (`agent_id`, `before` or `around`, `limit`) ->
  `transcript_entries` (`entries`, `first`, `last`, `available`): a page of the agent's
  conversation from its host copy, masked (`transcript_serve`), for running
  and stopped agents alike. `transcript_appended` (`agent_id`, `last`) tells
  the console new entries landed. `agent_started` and `agent_present` carry
  `transcript` when the agent's harness keeps one. `transcript_search`
  (`query`, `kinds`) -> `transcript_matches`: the newest matching entries'
  `seq` with a snippet, searched on the host over the masked text.

`iroh` (`bot-bottle[remote]`) is imported only here, only when the link runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ...contract.hostctl import ORIGIN_CONSOLE, LaunchRequest
from ...lib import log
from ...paths import bot_bottle_root
from ...util import same_instant
from .audit_index import query_host_journal
from .console_terminal import HISTORY_LINES, session_terminals, type_into, valid_size
from . import remote_actions
from .remote_requests import launch_extras, resume_request
from .transcript_serve import TranscriptLink

if TYPE_CHECKING:
    import iroh  # pyright: ignore[reportMissingImports]  # the optional `remote` extra

ALPN = b"bb-console/1"
ROLE_REMOTE = "remote"
CONSOLE_FILE = "console.json"
REMOTE_FILE = "remote.json"
STATUS_FILE = "remote.status.json"
KEY_FILE = "remote.key"
# How often remote.json is reread, and the reconnect backoff.
POLL_SECONDS = 3.0
BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
# How often launched agents are checked.
WATCH_SECONDS = 2.0
_REFUSED = 1  # application close codes
_HOST_STOPPING = 2
MAX_AUDIT_EVENTS = 500


class RemoteError(Exception):
    """Remote access can't be configured or can't run."""


# --- configuration ---


@dataclass(frozen=True)
class RemoteConfig:
    """`remote.json`: whether the link runs, and the pinned console identity."""

    enabled: bool = False
    console_node_id: str = ""
    console_ticket: str = ""


def _root() -> Path:
    return bot_bottle_root()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_config() -> RemoteConfig:
    try:
        raw = json.loads((_root() / REMOTE_FILE).read_text())
    except (OSError, ValueError):
        return RemoteConfig()
    if not isinstance(raw, dict):
        return RemoteConfig()
    return RemoteConfig(
        enabled=raw.get("enabled") is True,
        console_node_id=str(raw.get("console_node_id", "")),
        console_ticket=str(raw.get("console_ticket", "")),
    )


def save_config(config: RemoteConfig) -> None:
    _write_private(_root() / REMOTE_FILE, json.dumps(asdict(config), indent=2) + "\n")


def load_console() -> dict[str, Any]:
    """`console.json`, written by `bot-bottle pair`."""
    try:
        raw = json.loads((_root() / CONSOLE_FILE).read_text())
    except (OSError, ValueError) as e:
        raise RemoteError("not paired with a console; run `bot-bottle pair` first") from e
    if not isinstance(raw, dict) or not raw.get("url") or not raw.get("access_token"):
        raise RemoteError("console.json is incomplete; run `bot-bottle pair` again")
    return raw


def save_console(console: dict[str, Any]) -> None:
    _write_private(_root() / CONSOLE_FILE, json.dumps(console, indent=2) + "\n")


def secret_key() -> bytes:
    """This host's iroh secret key (32 bytes, 0600), created on first use. Its
    public half is the node id."""
    path = _root() / KEY_FILE
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def import_iroh() -> "Any":
    try:
        import iroh  # pylint: disable=import-outside-toplevel  # pyright: ignore[reportMissingImports]
    except ImportError as e:
        raise RemoteError(
            "remote access needs the iroh bindings: install `bot-bottle[remote]` "
            "(not available on Intel Macs)") from e
    return iroh


def node_id() -> str:
    """This host's iroh node id."""
    iroh = import_iroh()
    return str(iroh.SecretKey.from_bytes(secret_key()).public())


def write_status(**fields: object) -> None:
    """`remote.status.json`, for `bot-bottle status`."""
    try:
        _write_private(_root() / STATUS_FILE,
                       json.dumps({"updated": time.time(), **fields}) + "\n")
    except OSError:
        pass


def read_status() -> dict[str, Any]:
    try:
        raw = json.loads((_root() / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# --- the protocol ---


class Bottles(Protocol):
    """The part of `BottleService` the link drives."""

    def launch(self, request: LaunchRequest, role: str = ...) -> dict[str, object]: ...
    def teardown(self, bottle_id: str, role: str = ...) -> dict[str, object]: ...
    def session_state(self, session_id: str) -> dict[str, object]: ...
    def list(self) -> list[dict[str, object]]: ...


def terminal_agent_id(bottle: str, activation: str) -> str:
    """The console agent id of a session the console didn't launch: derived
    from its bottle and run, so a reconnect or a host controller restart
    names it the same without storing anything."""
    digest = hashlib.sha256(f"{bottle}\0{activation}".encode()).hexdigest()[:32]
    return f"t-{digest}"


def local_agents() -> list[str]:
    """The agents a remote launch may name: those under `~/.bot-bottle/`."""
    # pylint: disable=import-outside-toplevel
    from ...manifest import ManifestError, ManifestIndex
    try:
        # From $HOME, so only home agents.
        return ManifestIndex.resolve(str(Path.home()), missing_ok=True).all_agent_names
    except (ManifestError, OSError) as e:
        log.warn(f"remote: can't list local agents: {e}")
        return []


class Terminal(Protocol):
    """The relay of one session's terminal (`console_terminal.ConsoleTerminal`)."""

    writable: bool

    @property
    def closed(self) -> bool: ...
    def write(self, data: bytes) -> bool: ...
    def resize(self, cols: int, rows: int) -> None: ...
    def history(self, lines: int) -> tuple[str, int, int]: ...
    def close(self) -> None: ...


# console agent id, bottle id -> a started relay, or None without a session.
TerminalFactory = Callable[[str, str], Awaitable["Terminal | None"]]


@dataclass
class LinkState:
    """What survives reconnects: running agents (console id -> bottle id),
    every bottle shown, and the `agent_present` of each session the console
    didn't launch, to send again on reconnect."""

    running: dict[str, str] = field(default_factory=dict)
    launched: set[str] = field(default_factory=set)
    present: dict[str, dict[str, Any]] = field(default_factory=dict)


class ConsoleLink:
    """Serves one connection's commands. `send` writes one message; `state`
    outlives the connection (see `resume`)."""

    def __init__(self, bottles: Bottles, send: Callable[[dict[str, Any]], Awaitable[None]],
                 agents: Callable[[], list[str]] | None = None, *,
                 requests: Callable[[], list[dict[str, Any]]] | None = None,
                 orchestrator: Callable[[str], Any] | None = None,
                 terminals: TerminalFactory | None = None,
                 audit: Callable[[str, str | None, int], list[dict[str, Any]]] | None = None,
                 state: "LinkState | None" = None,
                 ) -> None:
        self._bottles = bottles
        self._send = send
        self._agents = agents if agents is not None else local_agents
        self._requests = requests if requests is not None else _pending_requests
        self._orchestrator = orchestrator if orchestrator is not None else _orchestrator
        self._terminal_factory = terminals
        self._audit = audit if audit is not None else query_host_journal
        state = state if state is not None else LinkState()
        # console agent id -> bottle id
        self.running = state.running
        # Every bottle shown (launched or present); only their journals may be
        # read.
        self.launched = state.launched
        # console agent id -> its `agent_present`, for sessions found running
        self.present = state.present
        # console agent id -> terminal relay
        self.terminals: dict[str, Terminal] = {}
        self._typed: dict[str, float] = {}  # when each was last typed into
        # console agent id -> the request raised for it
        self._asking: dict[str, Any] = {}
        # Bottles being torn down: still listed as running until they're gone,
        # but not to be presented again.
        self._stopping: set[str] = set()
        self._starting: set[str] = set()  # console agent ids mid-launch
        self.transcripts = TranscriptLink(self._send)

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "list_local":
            await self._send({"type": "local_definitions", "request_id": msg.get("request_id"),
                              "definitions": [{"name": n} for n in self._agents()]})
        elif kind == "launch":
            await self._launch(str(msg.get("agent_id", "")), msg.get("definition"), msg)
        elif kind == "resume":
            await self._resume(str(msg.get("agent_id", "")), msg.get("bottle"))
        elif kind == "stop":
            await self._stop(str(msg.get("agent_id", "")))
        elif kind == "approval":
            await self._approval(str(msg.get("agent_id", "")), str(msg.get("decision", "")),
                                 str(msg.get("since", "")))
        elif kind == "input":
            await self._input(str(msg.get("agent_id", "")), msg.get("data"))
        elif kind == "resize":
            self._resize(str(msg.get("agent_id", "")), msg.get("cols"), msg.get("rows"))
        elif kind == "history":
            await self._history(str(msg.get("agent_id", "")), msg.get("request_id"),
                                msg.get("lines"))
        elif kind == "audit_query":
            await self._audit_query(msg)
        elif kind in TranscriptLink.REQUESTS:
            await self.transcripts.handle(msg)
        elif kind in remote_actions.QUERIES:  # about one running agent
            await self._send(await remote_actions.QUERIES[kind](msg, self.running, self._bottles))
        elif kind == "forge_connect":
            await self._send(await remote_actions.forge_connect(msg))
        elif kind in remote_actions.ANSWERS:  # a card that takes more than allow/deny
            await self._send(await remote_actions.ANSWERS[kind](
                msg, self._asking, self._orchestrator, self._bottles))
        elif kind == "snapshot":
            await self._snapshot(msg)
        elif kind != "hello":  # hello only opens the stream
            log.debug(f"remote: ignoring console message {kind!r}")

    async def _errored(self, agent_id: str, message: str) -> None:
        await self._send({"type": "agent_errored", "agent_id": agent_id, "message": message})

    async def _launch(self, agent_id: str, definition: object, msg: dict[str, Any]) -> None:
        if not agent_id:
            return
        definition = definition if isinstance(definition, dict) else {}
        if "content" in definition:
            await self._errored(agent_id, "this host launches only its own agents, by name; "
                                          "console-defined agent content isn't accepted")
            return
        name = definition.get("name")
        if not isinstance(name, str) or name not in self._agents():
            await self._errored(agent_id, f"no local agent named {name!r} on this host")
            return
        extras = launch_extras(msg)
        if isinstance(extras, str):
            await self._errored(agent_id, extras)
            return
        prompt, bottles, label = extras
        # The console click is the launch confirmation.
        request = LaunchRequest(agent=name, assume_yes=True, prompt=prompt,
                                bottles=bottles, label=label, headless=False,
                                agent_id=agent_id)
        await self._start(agent_id, request)

    async def _resume(self, agent_id: str, bottle: object) -> None:
        """Relaunch a stopped bottle under its own identity, its conversation
        continuing (the console's Resume)."""
        if not agent_id:
            return
        request = resume_request(
            agent_id, bottle, agents=self._agents(),
            running=lambda b: b in self.running.values() or bool(
                self._bottles.session_state(b).get("running")))
        if isinstance(request, str):
            await self._errored(agent_id, request)
            return
        await self._start(agent_id, request)

    async def _start(self, agent_id: str, request: LaunchRequest) -> None:
        self._starting.add(agent_id)
        try:
            result = await asyncio.to_thread(self._bottles.launch, request, ROLE_REMOTE)
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            message = getattr(e, "message", "") or str(e) or type(e).__name__
            await self._errored(agent_id, f"launch failed: {message}"[:500])
            return
        finally:
            self._starting.discard(agent_id)
        bottle = str(result.get("bottle", ""))
        self.running[agent_id] = bottle
        self.launched.add(bottle)
        terminal = await self._open_terminal(agent_id, bottle)
        started: dict[str, Any] = {"type": "agent_started", "agent_id": agent_id,
                                   "bottle": bottle,
                                   "writable": bool(terminal and terminal.writable),
                                   "transcript": await self.transcripts.available(agent_id)}
        if result.get("activation"):
            started["activation"] = str(result["activation"])
        await self._send(started)

    async def _open_terminal(self, agent_id: str, bottle: str) -> "Terminal | None":
        if self._terminal_factory is None:
            return None
        try:
            terminal = await self._terminal_factory(agent_id, bottle)
        except Exception as e:  # pylint: disable=broad-exception-caught  # the agent still runs
            log.warn(f"remote: can't relay {bottle}'s terminal: {e}")
            return None
        if terminal is not None:
            self.terminals[agent_id] = terminal
        return terminal

    async def _live_terminal(self, agent_id: str) -> "Terminal | None":
        """The agent's relay, reopened if it has ended: a broken console stream
        ends it, and it would otherwise drop typing and output for good."""
        terminal = self.terminals.get(agent_id)
        if (terminal is None or terminal.closed) and agent_id in self.running:
            self.terminals.pop(agent_id, None)
            terminal = await self._open_terminal(agent_id, self.running[agent_id])
        return terminal

    def _close_terminal(self, agent_id: str) -> None:
        terminal = self.terminals.pop(agent_id, None)
        if terminal is not None:
            terminal.close()

    def close(self) -> None:
        """The console left: detach every relay (agents keep running)."""
        for agent_id in list(self.terminals):
            self._close_terminal(agent_id)

    async def _input(self, agent_id: str, data: object) -> None:
        terminal = await self._live_terminal(agent_id)
        if terminal is None or not isinstance(data, str) or not data:
            return
        typed = await type_into(terminal, data, self._typed.get(agent_id, 0.0))
        if typed is None:
            log.debug(f"remote: dropped input for {agent_id}: the agent doesn't allow typing")
        self._typed[agent_id] = typed or 0.0

    def _resize(self, agent_id: str, cols: object, rows: object) -> None:
        terminal = self.terminals.get(agent_id)
        size = valid_size(cols, rows)
        if terminal is not None and size is not None:
            terminal.resize(*size)

    async def _history(self, agent_id: str, request_id: object, lines: object) -> None:
        terminal = self.terminals.get(agent_id)
        # pylint: disable-next=unidiomatic-typecheck  # bool is an int
        count = lines if type(lines) is int and lines > 0 else HISTORY_LINES
        text, size, limit = ("", 0, 0) if terminal is None else \
            await asyncio.to_thread(terminal.history, count)
        await self._send({"type": "history", "request_id": request_id, "text": text,
                          "size": size, "limit": limit})

    async def _snapshot(self, msg: dict[str, Any]) -> None:
        """Snapshot a running agent's workspace, pinned to its conversation's
        newest entry."""
        # pylint: disable=import-outside-toplevel
        from .snapshots import SnapshotUnavailable
        agent_id = str(msg.get("agent_id", ""))
        reply: dict[str, Any] = {"type": "snapshot_taken", "request_id": msg.get("request_id"),
                                 "agent_id": agent_id}
        bottle = self.running.get(agent_id)
        try:
            if not bottle:
                raise SnapshotUnavailable("that agent isn't running on this host")
            snapshot = getattr(self._bottles, "snapshot", None)  # BottleService.snapshot
            if snapshot is None:
                raise SnapshotUnavailable("this host controller can't take snapshots")
            seq = await asyncio.to_thread(self.transcripts.reader.last, agent_id)
            reply["snapshot"] = await asyncio.to_thread(snapshot, bottle, seq=seq)
        except SnapshotUnavailable as e:
            reply["error"] = str(e)
        except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
            log.warn(f"remote: snapshot of {bottle} failed: {e}")
            reply["error"] = "the snapshot failed; see the host controller's log"
        await self._send(reply)

    async def push_transcripts(self) -> None:
        await self.transcripts.push(list(self.running))

    async def _audit_query(self, msg: dict[str, Any]) -> None:
        bottle, activation = msg.get("bottle"), msg.get("activation")
        limit = msg.get("limit")
        if type(limit) is not int or limit <= 0:  # pylint: disable=unidiomatic-typecheck
            limit = MAX_AUDIT_EVENTS
        limit = min(limit, MAX_AUDIT_EVENTS)
        events: list[dict[str, Any]] = []
        # Only these bottles' journals; anything else gets nothing, not an
        # error.
        if isinstance(bottle, str) and bottle in self.launched:
            activation = activation if isinstance(activation, str) and activation else None
            try:
                events = await asyncio.to_thread(self._audit, bottle, activation, limit)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.warn(f"remote: audit query for {bottle} failed: {e}")
        await self._send({"type": "audit_events", "request_id": msg.get("request_id"),
                          "events": events})

    async def _stop(self, agent_id: str) -> None:
        bottle = self.running.pop(agent_id, "")
        self.present.pop(agent_id, None)
        self._close_terminal(agent_id)
        if not bottle:
            return
        exit_code = 0
        self._stopping.add(bottle)
        try:
            result = await asyncio.to_thread(self._bottles.teardown, bottle, ROLE_REMOTE)
            exit_info = result.get("exit")
            if isinstance(exit_info, dict):
                exit_code = int(exit_info.get("status", 0) or 0)  # type: ignore[arg-type]
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            log.warn(f"remote: stopping {bottle} failed: {e}")
        finally:
            self._stopping.discard(bottle)
        await self._send({"type": "agent_stopped", "agent_id": agent_id, "exit_code": exit_code})

    async def resume(self) -> None:
        """A (re)connected console: report agents that exited, reopen running
        ones' relays, present the sessions it didn't launch, and send
        `agents_running` so the console can stop the rest."""
        await self.reap()
        for agent_id in list(self.running):
            await self._live_terminal(agent_id)
        for agent_id, present in list(self.present.items()):
            if agent_id in self.running:
                await self._send(present)
        await self.discover()
        await self._send({"type": "agents_running", "agent_ids": sorted(self.running)})

    async def discover(self) -> None:
        """Present each running session the console didn't launch (#717): the
        console shows every session on a paired host, wherever it started. A
        session the console did launch, which this link doesn't know (the host
        controller restarted), is taken up again under the console's id."""
        try:
            listed = await asyncio.to_thread(self._bottles.list)
        except Exception as e:  # pylint: disable=broad-exception-caught  # retried next round
            log.debug(f"remote: can't list sessions: {e}")
            return
        shown = set(self.running.values()) | self._stopping
        for entry in listed:
            bottle = str(entry.get("id", ""))
            session = entry.get("session")
            if (not bottle or bottle in shown
                    or not isinstance(session, dict) or not session.get("running")):
                continue
            if entry.get("origin") == ORIGIN_CONSOLE:
                # A launch in flight is recorded before it returns; it's _launch's.
                agent_id = str(entry.get("agent_id", ""))
                if agent_id and agent_id not in self._starting:
                    self.running[agent_id] = bottle
                    self.launched.add(bottle)
                    await self._open_terminal(agent_id, bottle)
                continue
            activation = str(entry.get("activation", ""))
            agent_id = terminal_agent_id(bottle, activation)
            self.running[agent_id] = bottle
            self.launched.add(bottle)
            terminal = await self._open_terminal(agent_id, bottle)
            present: dict[str, Any] = {
                "type": "agent_present", "agent_id": agent_id, "bottle": bottle,
                "label": str(entry.get("label", "")) or bottle,
                "definition": str(entry.get("agent", "")),
                "origin": str(entry.get("origin", "")),
                "writable": bool(terminal and terminal.writable),
                "transcript": await self.transcripts.available(agent_id)}
            if activation:
                present["activation"] = activation
            self.present[agent_id] = present
            await self._send(present)

    async def push_attention(self) -> None:
        """For each of the link's agents, raise its oldest pending request
        as `attention`, or `activity` once nothing waits. Only changes are
        sent."""
        # pylint: disable=import-outside-toplevel  # the link's only approvals use
        from ...approvals import Request, keep_target, route_lines
        try:
            pending = [Request.from_dict(raw) for raw in await asyncio.to_thread(self._requests)]
        except Exception as e:  # pylint: disable=broad-exception-caught  # retried next round
            log.debug(f"remote: can't list requests: {e}")
            return
        for agent_id, bottle in list(self.running.items()):
            request = next((r for r in pending if r.session == bottle), None)
            shown = self._asking.get(agent_id)
            if request is None:
                if shown is not None:
                    self._asking.pop(agent_id, None)
                    await self._send({"type": "activity", "agent_id": agent_id})
                continue
            if shown is not None and shown.id == request.id:
                continue
            self._asking[agent_id] = request
            lines = [request.summary(), *route_lines(request)]
            if request.justification.strip():
                lines.append(f"why: {request.justification.strip()}")
            target = keep_target(request.session) if request.can_save_to_project() else None
            if target is not None:
                lines.append(f"keep: in {target.label()}, from the next launch")
            options = ["allow_project"] if target is not None else []
            if (special := remote_actions.card(request)) is not None:
                lines, options = special
            await self._send({"type": "attention", "agent_id": agent_id, "reason": "approval",
                              "since": request.arrived, "message": "\n".join(lines),
                              "options": options})

    async def _approval(self, agent_id: str, decision: str, since: str) -> None:
        """The console's answer to `agent_id`'s raised approval. `since` must
        match the request still shown."""
        # pylint: disable=import-outside-toplevel  # the link's only approvals use
        from ...approvals import ALLOW_ONCE, ALLOW_PROJECT, DENY, answer
        from ..client import AlreadyAnswered
        request = self._asking.get(agent_id)
        chosen = {"allow": ALLOW_ONCE, "allow_project": ALLOW_PROJECT, "deny": DENY}.get(decision)
        if request is None or chosen is None or not same_instant(since, request.arrived):
            log.warn(f"remote: ignoring a stale or unknown approval for {agent_id}")
            return
        try:
            client = self._orchestrator(request.orchestrator)
            message = await asyncio.to_thread(answer, client, request, chosen,
                                              via="the phone")
            log.info(f"remote: {request.session}: {message}")
        except AlreadyAnswered as e:
            # Answered in the terminal first; it stands. The push below clears
            # the phone's prompt.
            log.info(f"remote: {request.session}: already answered ({e.decided_as}); "
                     "the phone's answer wasn't applied")
        except Exception as e:  # pylint: disable=broad-exception-caught  # logged; still pending
            log.warn(f"remote: couldn't answer {request.session}'s request: {e}")
            return
        await self.push_attention()

    async def reap(self) -> None:
        """Report agents that exited on their own."""
        for agent_id, bottle in list(self.running.items()):
            try:
                state = await asyncio.to_thread(self._bottles.session_state, bottle)
            except Exception:  # pylint: disable=broad-exception-caught  # retried next round
                continue
            if state.get("running"):
                await self._live_terminal(agent_id)
                continue
            self.running.pop(agent_id, None)
            self.present.pop(agent_id, None)
            self._close_terminal(agent_id)
            exit_info = state.get("exit")
            code = int(exit_info.get("status", 0) or 0) if isinstance(exit_info, dict) else 0  # type: ignore[arg-type]
            await self._send({"type": "agent_stopped", "agent_id": agent_id, "exit_code": code})


def _pending_requests() -> list[dict[str, Any]]:
    from .requests import orchestrator_pending  # pylint: disable=import-outside-toplevel
    return orchestrator_pending()


def _orchestrator(url: str = "") -> Any:
    """A client for the orchestrator holding a request (`url`), or the first
    found when it isn't known."""
    # pylint: disable=import-outside-toplevel  # only when a request is answered
    from ...contract.hostctl import Settings
    from ..client import OrchestratorClient, discover_orchestrator_url
    return OrchestratorClient(url or discover_orchestrator_url(Settings()))


# --- the connection ---


async def _read_lines(recv: "iroh.RecvStream") -> AsyncIterator[bytes]:
    buf = b""
    while True:
        chunk = await recv.read(4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                yield line


async def serve_connection(conn: "iroh.Connection", bottles: Bottles,
                           agents: Callable[[], list[str]] | None = None,
                           state: LinkState | None = None) -> None:
    """Serve one console connection until it closes. The link resumes at the
    first message (`hello`)."""
    control = await conn.accept_bi()
    send_stream = control.send()

    async def send(message: dict[str, Any]) -> None:
        await send_stream.write_all(json.dumps(message).encode() + b"\n")

    sessions = getattr(bottles, "sessions", None)
    link = ConsoleLink(bottles, send, agents, state=state,
                       terminals=session_terminals(conn, sessions) if sessions else None)
    await link.resume()

    async def watch() -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            await link.reap()
            await link.discover()
            await link.push_attention()
            await link.push_transcripts()

    watcher = asyncio.create_task(watch())
    try:
        async for line in _read_lines(control.recv()):
            try:
                msg = json.loads(line)
            except ValueError:
                log.warn("remote: dropped a malformed console message")
                continue
            if isinstance(msg, dict):
                await link.handle(msg)
    finally:
        watcher.cancel()
        link.close()


class _Stopped(Exception):
    """`stop` was set while the link was dialing or serving."""


async def _unless_stopped(coro: Awaitable[Any], stopped: "asyncio.Future[Any]") -> Any:
    """`coro`'s result, or `_Stopped` (with `coro` cancelled) once `stopped`
    resolves. `stopped` is shared and never cancelled here."""
    task = asyncio.ensure_future(coro)
    await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        return task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    raise _Stopped


async def run(config: RemoteConfig, bottles: Bottles, stop: threading.Event) -> None:
    """Dial the pinned console and serve it, reconnecting until `stop`. A
    `stop` while connected ends the connection at once, so the host
    controller shuts down without waiting on the console."""
    iroh = import_iroh()
    endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(
        preset=iroh.preset_n0(), secret_key=secret_key(), alpns=[]))
    attempt = 0
    state = LinkState()  # the link's agents, across reconnects
    # One waiter for the whole loop: a thread blocked on `stop` per attempt
    # would pile up across reconnects.
    stopped = asyncio.ensure_future(asyncio.to_thread(stop.wait))
    try:
        while not stop.is_set():
            conn = None
            try:
                addr = iroh.EndpointTicket.from_string(config.console_ticket).endpoint_addr()
                conn = await _unless_stopped(endpoint.connect(addr, ALPN), stopped)
                remote = str(conn.remote_id())
                if remote != config.console_node_id:
                    conn.close(_REFUSED, b"unexpected console")
                    raise RemoteError(f"console node id {remote[:10]} isn't the pinned one")
                attempt = 0
                write_status(connected=True, console_node_id=remote)
                log.info("remote: connected to the console")
                await _unless_stopped(serve_connection(conn, bottles, state=state), stopped)
                write_status(connected=False, error="console closed the connection")
            except _Stopped:
                if conn is not None:
                    conn.close(_HOST_STOPPING, b"host controller stopping")
                break
            except Exception as e:  # pylint: disable=broad-exception-caught
                write_status(connected=False, error=str(e)[:300] or type(e).__name__)
                log.debug(f"remote: link down: {e}")
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            await asyncio.to_thread(stop.wait, delay)
    finally:
        await endpoint.close()


class Supervisor:
    """Runs the link while `remote.json` enables it, rereading every
    `POLL_SECONDS`."""

    def __init__(self, bottles: Bottles) -> None:
        self._bottles = bottles
        self._closed = threading.Event()
        self._link: tuple[threading.Thread, threading.Event, RemoteConfig] | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="hostctl-remote").start()

    def close(self) -> None:
        self._closed.set()
        self._stop_link()

    def _loop(self) -> None:
        while not self._closed.is_set():
            config = load_config()
            current = self._link[2] if self._link else None
            if config != current:
                self._stop_link()
                if config.enabled and config.console_ticket and config.console_node_id:
                    self._start_link(config)
            self._closed.wait(POLL_SECONDS)

    def _start_link(self, config: RemoteConfig) -> None:
        stop = threading.Event()

        def target() -> None:
            try:
                asyncio.run(run(config, self._bottles, stop))
            except RemoteError as e:
                write_status(connected=False, error=str(e))
                log.warn(f"remote: {e}")

        thread = threading.Thread(target=target, daemon=True, name="hostctl-remote-link")
        self._link = (thread, stop, config)
        thread.start()

    def _stop_link(self) -> None:
        if self._link is None:
            return
        thread, stop, _config = self._link
        stop.set()
        thread.join(timeout=10)
        self._link = None
        write_status(connected=False, error="remote access disabled")


__all__ = [
    "ALPN", "ConsoleLink", "RemoteConfig", "RemoteError", "ROLE_REMOTE", "Supervisor",
    "load_config", "load_console", "node_id", "read_status", "save_config",
    "save_console", "secret_key", "serve_connection", "terminal_agent_id",
]
