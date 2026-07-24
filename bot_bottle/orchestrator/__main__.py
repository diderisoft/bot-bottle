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
from .store.store_manager import StoreManager
from .broker import LaunchBroker, StubBroker
from .control_plane import make_server
from .docker_broker import DockerBroker
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
    parser.add_argument(
        "--broker", choices=("stub", "docker"), default="stub",
        help="launch broker: 'stub' records requests; 'docker' runs containers",
    )
    args = parser.parse_args(argv)

    registry = RegistryStore(args.db)
    registry.migrate()
    # One DB per host: the supervise queue + audit tables live in the SAME
    # SQLite file the registry owns, so the control plane is the single
    # source of truth. The in-VM supervise daemon writes here; the host
    # operator reaches it over HTTP (never a second, disconnected DB).
    StoreManager(registry.db_path).migrate()

    # An ephemeral signing secret ties the orchestrator (signer) to its
    # broker (verifier). 'stub' records launches instead of starting
    # anything; 'docker' runs real containers (firecracker drops in later).
    secret = secrets.token_bytes(32)
    broker: LaunchBroker = DockerBroker(secret) if args.broker == "docker" else StubBroker(secret)
    orchestrator = Orchestrator(registry, broker, secret)

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
