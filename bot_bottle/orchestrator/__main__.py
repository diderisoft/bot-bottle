"""Run the orchestrator control plane.

    BOT_BOTTLE_ORCHESTRATOR_TOKEN=<signing-key> \\
        python -m bot_bottle.orchestrator --db PATH [--host H] [--port P]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..lib import log
from ..contract.auth import CONTROL_PLANE
from .server import make_server
from .service import OrchestratorCore
from .store.store_manager import StoreManager
from .store.registry_store import RegistryStore


def main(argv: list[str] | None = None) -> int:
    """Parse args, migrate the registry, and serve the control plane."""
    parser = argparse.ArgumentParser(prog="bot_bottle.orchestrator")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8080, help="bind port (0 = ephemeral)")
    parser.add_argument(
        "--db", type=Path, required=True,
        help="the shared state DB (registry, supervise queue + audit)",
    )
    args = parser.parse_args(argv)
    if not CONTROL_PLANE.key_from_env():
        log.die(
            f"{CONTROL_PLANE.key_env} is required; refusing to start the "
            "orchestrator without caller authentication"
        )

    registry = RegistryStore(args.db)
    registry.migrate()
    # One DB per host: the supervise and audit tables share the registry's
    # file.
    StoreManager(registry.db_path).migrate()

    orchestrator = OrchestratorCore(registry)

    server = make_server(orchestrator, host=args.host, port=args.port)
    log.info(
        "orchestrator control plane listening",
        context={"host": args.host, "port": args.port, "db": str(registry.db_path)},
    )
    try:
        server.run()
    except KeyboardInterrupt:
        log.info("orchestrator shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
