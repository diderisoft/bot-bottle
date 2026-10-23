"""Remote access: the host end of the console link over iroh (ADR 0007).

When remote access is enabled (`bot-bottle remote enable`), the host
controller dials the console it logged in to, over iroh, on ALPN
`bb-console/1`. The link is dial-out only: the host's endpoint accepts no
inbound connections, and it refuses a console whose node id isn't the one
pinned at login. The console identifies the host by its iroh node id, which
`remote enable` bound to the host with its access token.

On the console's control stream (JSON lines) the host serves:

- `list_local` -> `local_definitions`: the agents under `~/.bot-bottle/`.
- `launch` -> runs a **local agent by name** (`definition.name`) through
  `BottleService`, as role `remote`, and reports `agent_started` or
  `agent_errored`. Console-supplied agent content is refused: bottles and the
  agents that pick them come from this host, never from a remote party.
  The launch may carry `prompt` (the first message of an interactive
  session), `bottles` (names of this host's bottles) and `label`.
  `agent_started` reports the bottle, the run's `activation` and whether the
  console may type into the session (`writable`, the agent's
  `remote.writable`).
- `stop` -> tears the bottle down; `agent_stopped` follows. An agent that
  exits on its own is reported `agent_stopped` too.
- The session's terminal is relayed to the console on a uni stream (see
  `console_terminal`); `input` types into it when the agent allows remote
  typing, and `resize` sizes the console's tmux client.
- `audit_query` -> `audit_events`: the audit journal records for a bottle this
  link launched, filtered like `audit query` (bottle, activation, limit).

`iroh` is an optional dependency (`bot-bottle[remote]`), imported only here
and only when the link runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ...contract.hostctl import LaunchRequest
from ...lib import log
from ...paths import bot_bottle_root
from .console_terminal import valid_size

if TYPE_CHECKING:
    import iroh  # pyright: ignore[reportMissingImports]  # the optional `remote` extra

ALPN = b"bb-console/1"
ROLE_REMOTE = "remote"
CONSOLE_FILE = "console.json"
REMOTE_FILE = "remote.json"
STATUS_FILE = "remote.status.json"
KEY_FILE = "remote.key"
# How often the supervisor rereads remote.json, and the reconnect backoff.
POLL_SECONDS = 3.0
BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
# How often the link checks whether launched agents are still running.
WATCH_SECONDS = 2.0
_REFUSED = 1  # application close code
# Bounds on what a console launch may carry.
MAX_PROMPT_CHARS = 20_000
MAX_BOTTLES = 8
MAX_LABEL_CHARS = 128
MAX_AUDIT_EVENTS = 500
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RemoteError(Exception):
    """Remote access can't be configured or can't run."""


# -- configuration -------------------------------------------------------------


@dataclass(frozen=True)
class RemoteConfig:
    """`remote.json`: whether the link runs, and the console identity pinned
    for it."""

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
    """`console.json`, written by `bot-bottle login`."""
    try:
        raw = json.loads((_root() / CONSOLE_FILE).read_text())
    except (OSError, ValueError) as e:
        raise RemoteError("not logged in to a console; run `bot-bottle login` first") from e
    if not isinstance(raw, dict) or not raw.get("url") or not raw.get("access_token"):
        raise RemoteError("console.json is incomplete; run `bot-bottle login` again")
    return raw


def save_console(console: dict[str, Any]) -> None:
    _write_private(_root() / CONSOLE_FILE, json.dumps(console, indent=2) + "\n")


def secret_key() -> bytes:
    """This host's iroh secret key (32 bytes, mode 0600), created on first
    use. Its public half is the node id the console knows the host by."""
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
    """`remote.status.json`, for `bot-bottle remote status`."""
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


# -- the protocol --------------------------------------------------------------


class Bottles(Protocol):
    """The part of `BottleService` the link drives."""

    def launch(self, request: LaunchRequest, role: str = ...) -> dict[str, object]: ...
    def teardown(self, bottle_id: str, role: str = ...) -> dict[str, object]: ...
    def session_state(self, session_id: str) -> dict[str, object]: ...


def local_agents() -> list[str]:
    """The agents a remote launch may name: those under `~/.bot-bottle/`."""
    # pylint: disable=import-outside-toplevel
    from ...manifest import ManifestError, ManifestIndex
    try:
        # Resolved from $HOME, so only home agents: a repo's agents need its cwd.
        return ManifestIndex.resolve(str(Path.home()), missing_ok=True).all_agent_names
    except (ManifestError, OSError) as e:
        log.warn(f"remote: can't list local agents: {e}")
        return []


class Terminal(Protocol):
    """The relay of one session's terminal (`console_terminal.ConsoleTerminal`)."""

    writable: bool

    def write(self, data: bytes) -> bool: ...
    def resize(self, cols: int, rows: int) -> None: ...
    def close(self) -> None: ...


# console agent id, bottle id -> a started relay, or None if there's no session.
TerminalFactory = Callable[[str, str], Awaitable["Terminal | None"]]


def _launch_extras(msg: dict[str, Any]) -> tuple[str, tuple[str, ...], str] | str:
    """A console launch's optional prompt, bottles and label, or the reason
    they're refused."""
    prompt = msg.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        return f"the prompt must be text of at most {MAX_PROMPT_CHARS} characters"
    bottles = msg.get("bottles", [])
    if (not isinstance(bottles, list) or len(bottles) > MAX_BOTTLES
            or not all(isinstance(b, str) and _NAME.match(b) for b in bottles)):
        return f"bottles must be a list of at most {MAX_BOTTLES} bottle names"
    label = msg.get("label", "")
    if not isinstance(label, str) or len(label) > MAX_LABEL_CHARS:
        return f"the label must be text of at most {MAX_LABEL_CHARS} characters"
    return prompt, tuple(bottles), label


def query_audit(bottle: str, activation: str | None, limit: int) -> list[dict[str, Any]]:
    """`audit query --bottle --activation --limit` against this host's journal;
    empty when there's no journal yet."""
    # pylint: disable=import-outside-toplevel
    from ...paths import host_audit_dir, host_db_path
    from .audit_index import AuditIndex, Query

    directory = host_audit_dir()
    if not directory.is_dir():
        return []
    index = AuditIndex(directory, host_db_path())
    index.sync()
    return index.query(Query(bottle=bottle, activation=activation, limit=limit))


def session_terminals(conn: "iroh.Connection", sessions: Any) -> TerminalFactory:
    """Relays for this connection: each opens a uni stream to the console and
    attaches to the agent's tmux session."""
    from .console_terminal import ConsoleTerminal  # pylint: disable=import-outside-toplevel

    async def start(agent_id: str, bottle: str) -> Terminal | None:
        record = sessions.record(bottle)
        if record is None or not sessions.exists(bottle):
            return None
        terminal = ConsoleTerminal(sessions, bottle, agent_id,
                                   writable_allowed=record.writable_allowed)
        await terminal.start(conn.open_uni)
        return terminal

    return start


class ConsoleLink:
    """Serves the console's commands for one connection. Transport-free: `send`
    writes one message to the console."""

    def __init__(self, bottles: Bottles, send: Callable[[dict[str, Any]], Awaitable[None]],
                 agents: Callable[[], list[str]] | None = None, *,
                 terminals: TerminalFactory | None = None,
                 audit: Callable[[str, str | None, int], list[dict[str, Any]]] | None = None,
                 ) -> None:
        self._bottles = bottles
        self._send = send
        self._agents = agents if agents is not None else local_agents
        self._terminal_factory = terminals
        self._audit = audit if audit is not None else query_audit
        # console agent id -> bottle id, for the agents this link launched.
        self.running: dict[str, str] = {}
        # Every bottle this link launched, running or not: the console may
        # read the journal of these, and only these.
        self.launched: set[str] = set()
        # console agent id -> the relay of its session's terminal.
        self.terminals: dict[str, Terminal] = {}

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "list_local":
            await self._send({"type": "local_definitions", "request_id": msg.get("request_id"),
                              "definitions": [{"name": n} for n in self._agents()]})
        elif kind == "launch":
            await self._launch(str(msg.get("agent_id", "")), msg.get("definition"), msg)
        elif kind == "stop":
            await self._stop(str(msg.get("agent_id", "")))
        elif kind == "input":
            self._input(str(msg.get("agent_id", "")), msg.get("data"))
        elif kind == "resize":
            self._resize(str(msg.get("agent_id", "")), msg.get("cols"), msg.get("rows"))
        elif kind == "audit_query":
            await self._audit_query(msg)
        else:
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
        extras = _launch_extras(msg)
        if isinstance(extras, str):
            await self._errored(agent_id, extras)
            return
        prompt, bottles, label = extras
        # The operator chose this local agent in the console: that click is the
        # launch confirmation a terminal would otherwise ask for. A prompt
        # starts an interactive session with it as the first message.
        request = LaunchRequest(agent=name, assume_yes=True, prompt=prompt,
                                bottles=bottles, label=label, headless=False)
        try:
            result = await asyncio.to_thread(self._bottles.launch, request, ROLE_REMOTE)
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            message = getattr(e, "message", "") or str(e) or type(e).__name__
            await self._errored(agent_id, f"launch failed: {message}"[:500])
            return
        bottle = str(result.get("bottle", ""))
        self.running[agent_id] = bottle
        self.launched.add(bottle)
        terminal = await self._open_terminal(agent_id, bottle)
        started: dict[str, Any] = {"type": "agent_started", "agent_id": agent_id,
                                   "bottle": bottle,
                                   "writable": bool(terminal and terminal.writable)}
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

    def _close_terminal(self, agent_id: str) -> None:
        terminal = self.terminals.pop(agent_id, None)
        if terminal is not None:
            terminal.close()

    def close(self) -> None:
        """The console went away: detach every relay (the agents keep running)."""
        for agent_id in list(self.terminals):
            self._close_terminal(agent_id)

    def _input(self, agent_id: str, data: object) -> None:
        terminal = self.terminals.get(agent_id)
        if terminal is None or not isinstance(data, str) or not data:
            return
        if not terminal.write(data.encode()):
            log.debug(f"remote: dropped input for {agent_id}: "
                      "the agent doesn't allow remote typing")

    def _resize(self, agent_id: str, cols: object, rows: object) -> None:
        terminal = self.terminals.get(agent_id)
        size = valid_size(cols, rows)
        if terminal is not None and size is not None:
            terminal.resize(*size)

    async def _audit_query(self, msg: dict[str, Any]) -> None:
        bottle, activation = msg.get("bottle"), msg.get("activation")
        limit = msg.get("limit")
        if type(limit) is not int or limit <= 0:  # pylint: disable=unidiomatic-typecheck
            limit = MAX_AUDIT_EVENTS
        limit = min(limit, MAX_AUDIT_EVENTS)
        events: list[dict[str, Any]] = []
        # Only the journal of agents this console launched; anything else is
        # answered with nothing rather than an error that confirms it exists.
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
        self._close_terminal(agent_id)
        if not bottle:
            return
        exit_code = 0
        try:
            result = await asyncio.to_thread(self._bottles.teardown, bottle, ROLE_REMOTE)
            exit_info = result.get("exit")
            if isinstance(exit_info, dict):
                exit_code = int(exit_info.get("status", 0) or 0)  # type: ignore[arg-type]
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            log.warn(f"remote: stopping {bottle} failed: {e}")
        await self._send({"type": "agent_stopped", "agent_id": agent_id, "exit_code": exit_code})

    async def reap(self) -> None:
        """Report agents that exited on their own."""
        for agent_id, bottle in list(self.running.items()):
            try:
                state = await asyncio.to_thread(self._bottles.session_state, bottle)
            except Exception:  # pylint: disable=broad-exception-caught  # retried next round
                continue
            if state.get("running"):
                continue
            self.running.pop(agent_id, None)
            self._close_terminal(agent_id)
            exit_info = state.get("exit")
            code = int(exit_info.get("status", 0) or 0) if isinstance(exit_info, dict) else 0  # type: ignore[arg-type]
            await self._send({"type": "agent_stopped", "agent_id": agent_id, "exit_code": code})


# -- the connection ------------------------------------------------------------


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
                           agents: Callable[[], list[str]] | None = None) -> None:
    """Serve one console connection until it closes. The console opens the
    control stream; it becomes visible here with its first command."""
    control = await conn.accept_bi()
    send_stream = control.send()

    async def send(message: dict[str, Any]) -> None:
        await send_stream.write_all(json.dumps(message).encode() + b"\n")

    sessions = getattr(bottles, "sessions", None)
    link = ConsoleLink(bottles, send, agents,
                       terminals=session_terminals(conn, sessions) if sessions else None)

    async def watch() -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            await link.reap()

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


async def run(config: RemoteConfig, bottles: Bottles, stop: threading.Event) -> None:
    """Dial the pinned console and serve it, reconnecting until `stop`."""
    iroh = import_iroh()
    endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(
        preset=iroh.preset_n0(), secret_key=secret_key(), alpns=[]))
    attempt = 0
    try:
        while not stop.is_set():
            try:
                addr = iroh.EndpointTicket.from_string(config.console_ticket).endpoint_addr()
                conn = await endpoint.connect(addr, ALPN)
                remote = str(conn.remote_id())
                if remote != config.console_node_id:
                    conn.close(_REFUSED, b"unexpected console")
                    raise RemoteError(f"console node id {remote[:10]} isn't the pinned one")
                attempt = 0
                write_status(connected=True, console_node_id=remote)
                log.info("remote: connected to the console")
                await serve_connection(conn, bottles)
                write_status(connected=False, error="console closed the connection")
            except Exception as e:  # pylint: disable=broad-exception-caught
                write_status(connected=False, error=str(e)[:300] or type(e).__name__)
                log.debug(f"remote: link down: {e}")
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            await asyncio.to_thread(stop.wait, delay)
    finally:
        await endpoint.close()


class Supervisor:
    """Runs the link while `remote.json` says it's enabled. Rereads the file
    every `POLL_SECONDS`, so `remote enable`/`disable` take effect without
    restarting the host controller."""

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
    "save_console", "secret_key", "serve_connection",
]
