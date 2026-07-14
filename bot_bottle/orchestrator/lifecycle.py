"""Orchestrator process lifecycle (PRD 0070, docker slice).

Before the CLI can register or launch bottles against the consolidated
model, exactly one orchestrator control plane — and the single per-host
gateway it manages — must be running. This starts the orchestrator
dev-harness (`python -m bot_bottle.orchestrator`) as a background host
process and health-checks it.

It is an **idempotent singleton**: `ensure_running` returns immediately if a
healthy control plane already answers on the port, and otherwise spawns one
and waits for it to come up. The control-plane port is the singleton key —
a second orchestrator can't bind it, so a stray double-start fails fast
rather than forking a rival.

Host-process (not container) on purpose: the PRD sequences the orchestrator
as a plain-process dev-harness first (fast iteration, and it already has the
host user's docker access to broker launches), while the data-plane
*gateway* it manages runs as a container. Wrapping the orchestrator itself
in a backend-native unit is a later step.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request

from .. import log
from ..paths import bot_bottle_root

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
# Poll cadence + default ceiling while waiting for a freshly-spawned control
# plane to answer /health (the first start also builds/boots the gateway).
_HEALTH_POLL_SECONDS = 0.25
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0


class OrchestratorStartError(RuntimeError):
    """The orchestrator process did not become healthy within the timeout."""


class OrchestratorProcess:
    """Manages the local orchestrator control-plane process for the docker
    backend. Backend-neutral callers only need `ensure_running()` + `url`."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        broker: str = "docker",
        gateway: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self._broker = broker
        self._gateway = gateway

    @property
    def url(self) -> str:
        """The control-plane base URL — also what the data plane's
        BOT_BOTTLE_ORCHESTRATOR_URL points at."""
        return f"http://{self.host}:{self.port}"

    def is_healthy(self, *, timeout: float = _HEALTH_REQUEST_TIMEOUT_SECONDS) -> bool:
        """True iff a control plane answers `GET /health` with 200 — the
        singleton liveness check."""
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Return the control-plane URL, starting the orchestrator first if it
        isn't already healthy. Idempotent — a healthy control plane is left
        untouched. Raises `OrchestratorStartError` if a freshly-spawned one
        doesn't answer within `startup_timeout`."""
        if self.is_healthy():
            return self.url
        log.info("starting orchestrator", context={"url": self.url, "broker": self._broker})
        self._spawn()
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self.is_healthy():
                log.info("orchestrator healthy", context={"url": self.url})
                return self.url
            time.sleep(_HEALTH_POLL_SECONDS)
        raise OrchestratorStartError(
            f"orchestrator at {self.url} did not become healthy within "
            f"{startup_timeout:g}s"
        )

    def _argv(self) -> list[str]:
        """`python -m bot_bottle.orchestrator ...` — static flags only."""
        argv = [
            sys.executable, "-m", "bot_bottle.orchestrator",
            "--host", self.host, "--port", str(self.port),
            "--broker", self._broker,
        ]
        if self._gateway:
            argv.append("--gateway")
        return argv

    def _log_path(self) -> str:
        """Where the detached orchestrator's stdout/stderr goes so a failed
        start is diagnosable after the CLI has moved on."""
        return str(bot_bottle_root() / "orchestrator.log")

    def _spawn(self) -> None:
        """Launch the orchestrator detached so it outlives this CLI process,
        with its output tee'd to a log file under the bot-bottle root."""
        root = bot_bottle_root()
        root.mkdir(parents=True, exist_ok=True)
        logfile = open(self._log_path(), "a", encoding="utf-8")  # noqa: SIM115  # pylint: disable=consider-using-with
        try:
            subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
                self._argv(),
                stdout=logfile,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            # The child inherits its own dup'd fd; this handle is ours to drop.
            logfile.close()


__all__ = [
    "OrchestratorProcess",
    "OrchestratorStartError",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
]
