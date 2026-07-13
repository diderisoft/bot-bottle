"""Run the orchestrator control plane as a plain process (PRD 0070 dev-harness).

    python -m bot_bottle.orchestrator [--host H] [--port P] [--db PATH]

The PRD sequences the orchestrator as a plain-process dev-harness first, so
the consolidation core (registry + attribution + HTTP control plane + live
reload) can be exercised with fast iteration, decoupled from any VM /
container packaging. Wrapping this exact service in a backend-native unit
(docker container, then Firecracker VM) comes later.
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from .. import log
from .broker import StubBroker
from .control_plane import make_server
from .registry import RegistryStore, default_db_path
from .service import Orchestrator


def main(argv: list[str] | None = None) -> int:
    """Parse args, migrate the registry, and serve the control plane."""
    parser = argparse.ArgumentParser(prog="bot_bottle.orchestrator")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8080, help="bind port (0 = ephemeral)")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"registry DB path (default: {default_db_path()})",
    )
    args = parser.parse_args(argv)

    registry = RegistryStore(args.db)
    registry.migrate()

    # Dev-harness wiring: an ephemeral signing secret + a stub broker that
    # records launches instead of starting anything. A real backend broker
    # (docker, then firecracker) drops in here later.
    secret = secrets.token_bytes(32)
    orchestrator = Orchestrator(registry, StubBroker(secret), secret)

    server = make_server(orchestrator, host=args.host, port=args.port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    log.info(
        "orchestrator control plane listening",
        context={"host": bound_host, "port": bound_port, "db": str(registry.db_path)},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("orchestrator shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
