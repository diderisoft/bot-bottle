"""Rotate every backend's gateway mitmproxy CA (issue #450).

    python -m bot_bottle.orchestrator.rotate_ca

For each backend available on this host, under its bring-up lock: stop the
gateway (whose mitmproxy holds the CA in memory), then delete the persisted CA
(`Gateway.persisted_ca_dir`) so the next start mints a fresh one and
reattaches every running bottle to it (PRD 0081). Backends may share a CA dir;
a dir is cleared only once every gateway using it has stopped, so a gateway
that can't be stopped keeps serving a CA that still exists on disk. Running
bottles lose egress until the next bring-up.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from ..gateway import GatewayError, rotate_gateway_ca

if TYPE_CHECKING:
    from ..backend.infra_service import InfraService


def _out(msg: str) -> None:
    sys.stdout.write(f"rotate-ca: {msg}\n")


def _services() -> list[tuple[str, Callable[[], "InfraService"]]]:
    """(backend name, infra service factory) for every registered backend
    available on this host. Driven by the backend registry, so a new backend
    is covered without changes here."""
    from ..backend import get_bottle_backend, has_backend, known_backend_names
    return [
        (name, lambda name=name: get_bottle_backend(name).infra_service())
        for name in sorted(known_backend_names())
        if has_backend(name)
    ]


def _stop_gateway(name: str, service: "InfraService") -> bool:
    try:
        gateway = service.gateway()
        gateway.stop()
        if gateway.is_running():
            raise GatewayError("it is still running")
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Any backend can fail in its own way; report it and keep its CA.
        _out(f"{name}: could not stop the gateway: {e}")
        return False
    _out(f"{name}: stopped the gateway")
    return True


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags — a single deliberate action
    services = [(name, factory()) for name, factory in _services()]
    if not services:
        _out("no backend available on this host; nothing to rotate")
        return 0
    users: dict[Path | None, list[str]] = {}
    stopped: dict[str, bool] = {}
    with ExitStack() as locks:
        for _, service in services:
            locks.enter_context(service.bring_up_lock())
        for name, service in services:
            ca_dir = service.gateway().persisted_ca_dir()
            users.setdefault(ca_dir.resolve() if ca_dir else None, []).append(name)
            stopped[name] = _stop_gateway(name, service)
        for ca_dir, names in users.items():
            if ca_dir is None:
                _out(f"{', '.join(names)}: CA isn't in a host dir (named volume); "
                     "remove it to rotate")
                continue
            if blocked := [n for n in names if not stopped[n]]:
                _out(f"kept the CA in {ca_dir}: the {', '.join(blocked)} gateway "
                     "could not be stopped")
                continue
            removed = rotate_gateway_ca(ca_dir)
            _out(f"{', '.join(names)}: removed {len(removed)} CA file(s) from {ca_dir}"
                 if removed else f"{', '.join(names)}: no persisted CA under {ca_dir}")
    if not all(stopped.values()):
        _out("incomplete — fix the errors above and run it again")
        return 1
    _out("done — the next bottle launch mints the new CA and reattaches running "
         "bottles to it; agents that load their CA once at start (Node's "
         "NODE_EXTRA_CA_CERTS) need a restart to trust it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
