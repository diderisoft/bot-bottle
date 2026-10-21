"""`python -m bot_bottle.control.hostctl`: run the host controller daemon.

Binds 127.0.0.1 (port from `BOT_BOTTLE_HOSTCTL_PORT`, else ephemeral),
publishes its state file, and serves until SIGTERM or SIGINT. It also
listens on each backend's host address for the orchestrators it brings up
(see `listeners`).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType

from ...contract.auth import ROLE_GATEWAY
from ...contract.hostctl import GUEST_PORT, HOST_CONTROL, PORT_ENV, ROLE_ORCHESTRATOR
from ...control.lifecycle import Orchestrator
from ...lib.log import debug, info
from ...trust_domain import HostControlProvisioning, HostLink, signing_key
from .build import current_build_id
from .runner import HostControlState, clear_state, write_state
from . import localsock
from .bottles import BottleService
from .jobs import JobRegistry
from .listeners import GuestListeners
from .server import HostControlServer
from .sessions import SessionManager, control_socket_path, tmux_version, write_config

# What a guest listener accepts: never `cli`, which only the host holds.
GUEST_ROLES = frozenset({ROLE_ORCHESTRATOR, ROLE_GATEWAY})


def serve_running_orchestrators() -> None:
    """Listen for the orchestrators already up from an earlier daemon: each
    backend whose infra was brought up before, on its recorded settings."""
    # pylint: disable=import-outside-toplevel
    from ...backend import get_bottle_backend
    from ...backend.infra_service import recorded_infra_settings
    from ...backend.selection import BACKEND_NAMES
    from ...contract.hostctl import Settings

    for name in BACKEND_NAMES:
        infra = recorded_infra_settings(name)
        if infra is None:
            continue
        try:
            orchestrator = get_bottle_backend(
                Settings(infra=infra), name, prompt=False).infra_service().orchestrator()
            if orchestrator.is_running():
                orchestrator.serve_host_control()
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            debug(f"host controller: no {name} orchestrator to listen for: {e}")


def main() -> int:
    port = int(os.environ.get(PORT_ENV, "0") or "0")
    sessions = SessionManager({})
    # Registers the `bottle` finisher, so it must exist before adopting.
    bottles = BottleService(sessions)
    local = localsock.serve(control_socket_path(), sessions)
    if tmux_version() is not None:
        write_config()
        sessions.adopt()
    build_id, key, jobs = current_build_id(), signing_key(HOST_CONTROL), JobRegistry()
    server = HostControlServer(("127.0.0.1", port), build_id, key, sessions, bottles, jobs=jobs)

    def guest_server(link: HostLink) -> HostControlServer:
        return HostControlServer((link.address, GUEST_PORT), build_id, key, sessions, bottles,
                                 jobs=jobs, peers=link.network, roles=GUEST_ROLES)

    guests = GuestListeners(guest_server)
    # Every orchestrator this process brings up gets this daemon's URL on its
    # link and an `orchestrator` token.
    Orchestrator.host_control = HostControlProvisioning(listen=guests.listen)
    threading.Thread(target=serve_running_orchestrators, daemon=True).start()
    state = HostControlState(
        pid=os.getpid(), port=server.server_address[1], build_id=server.build_id
    )

    def _shutdown(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        # shutdown() blocks until serve_forever returns, so not from its thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    write_state(state)
    info(f"host controller listening on {state.url} (pid {state.pid}, build {state.build_id})")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        guests.close()
        local.close()
        clear_state(state.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
