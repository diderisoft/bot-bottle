"""`python -m bot_bottle.control.hostctl`: run the host controller daemon.

Binds 127.0.0.1 (port from `BOT_BOTTLE_HOSTCTL_PORT`, else ephemeral),
publishes its state file, and serves until SIGTERM or SIGINT.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType

from ...contract.hostctl import HOST_CONTROL, PORT_ENV
from ...lib.log import info
from ...trust_domain import signing_key
from .build import current_build_id
from .runner import HostControlState, clear_state, write_state
from .server import HostControlServer


def main() -> int:
    port = int(os.environ.get(PORT_ENV, "0") or "0")
    server = HostControlServer(("127.0.0.1", port), current_build_id(), signing_key(HOST_CONTROL))
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
        clear_state(state.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
