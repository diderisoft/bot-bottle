"""How this process reaches the host DB's state (ADR 0020).

The host controller is the host DB's only writer. It calls `use_stores()` as
it starts, and from then on code shared with the CLI (forge connections,
trust records) reads and writes the stores directly. Any other process, the
CLI above all, asks the host controller over its API (`client()`), starting
it if it isn't running, so it never opens the file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .control.hostctl.client import HostControlClient

_stores = False


def use_stores() -> None:
    """This process is the host controller: shared code uses the stores."""
    global _stores  # pylint: disable=global-statement
    _stores = True


def in_host_controller() -> bool:
    return _stores


def client() -> "HostControlClient":
    """The running host controller's `cli` client, started if need be."""
    # pylint: disable=import-outside-toplevel
    from .control.hostctl import runner
    from .control.hostctl.client import HostControlClient
    return HostControlClient(runner.ensure_running(), runner.cli_token())


def ask(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    """The host controller's answer on `path`: GET without a `body`, else
    POST it. Raises `HostControlError` when it refuses or can't be reached."""
    reached = client()
    return reached.get(path) if body is None else reached.post(path, body)


def ask_running(path: str) -> dict[str, object] | None:
    """GET `path` of the running host controller; None when none runs. For a
    reader that mustn't start one."""
    # pylint: disable=import-outside-toplevel
    from .control.hostctl import runner
    from .control.hostctl.client import HostControlClient
    state = runner.running()
    if state is None:
        return None
    return HostControlClient(state, runner.cli_token()).get(path)


__all__ = ["ask", "ask_running", "client", "in_host_controller", "use_stores"]
