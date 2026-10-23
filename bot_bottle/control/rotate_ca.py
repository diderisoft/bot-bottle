"""Rotate every backend's gateway mitmproxy CA.

    python -m bot_bottle.control.rotate_ca

Under every backend's bring-up lock, backends are grouped by the CA dir their
gateways share (docker and macOS share one). For a group with a running
gateway, the CA is rolled over through it to a new one cross-signed by the
current one, then each running gateway is restarted and its bottles reattached:
agents that loaded the old CA keep validating, new ones trust the
new CA. A group with no running gateway has its persisted CA cleared, so the
next start mints a fresh one. A failed group keeps its CA and the command
exits non-zero; the others still rotate.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from .gateway import GatewayError, rotate_gateway_ca

if TYPE_CHECKING:
    from ..backend.infra_service import InfraService
    from ..contract.hostctl import Settings


def _out(msg: str) -> None:
    sys.stdout.write(f"rotate-ca: {msg}\n")


def _services(settings: "Settings") -> list[tuple[str, Callable[[], "InfraService"]]]:
    """(backend name, infra service factory) for every registered backend
    available on this host. Driven by the backend registry, so a new backend
    is covered without changes here."""
    from ..backend import get_bottle_backend, has_backend, known_backend_names
    return [
        (name, lambda name=name: get_bottle_backend(settings, name).infra_service())
        for name in sorted(known_backend_names())
        if has_backend(settings, name)
    ]


def _rotate_group(ca_dir: Path | None, members: dict[str, "InfraService"]) -> None:
    """Rotate the CA one or more backends share. Raises on failure."""
    running = [name for name, service in members.items() if service.gateway().is_running()]
    if not running:
        label = ", ".join(members)
        if ca_dir is None:
            _out(f"{label}: gateway isn't running and its CA is in a named volume; "
                 "remove the volume to rotate")
            return
        removed = rotate_gateway_ca(ca_dir)
        _out(f"{label}: gateway isn't running; removed {len(removed)} CA file(s) from "
             f"{ca_dir}, so its next start mints a new CA")
        return
    # The rollover runs the gateway's own installed code, so bring it up to
    # date first (a no-op when it's current).
    members[running[0]].ensure_running()
    members[running[0]].gateway().roll_over_ca()
    _out(f"{running[0]}: rolled the CA over to a new one cross-signed by the current one")
    for name in running:
        gateway = members[name].gateway()
        gateway.stop()
        if gateway.is_running():
            raise GatewayError(f"the {name} gateway is still running after stop")
    for name in running:
        members[name].ensure_running()
        _out(f"{name}: restarted the gateway on the new CA and reattached running bottles")


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags — a single deliberate action
    from ..cli.settings import settings_from_env
    services = [(name, factory()) for name, factory in _services(settings_from_env())]
    if not services:
        _out("no backend available on this host; nothing to rotate")
        return 0
    groups: dict[Path | str, tuple[Path | None, dict[str, "InfraService"]]] = {}
    for name, service in services:
        ca_dir = service.gateway().persisted_ca_dir()
        key: Path | str = ca_dir.resolve() if ca_dir else f"volume:{name}"
        groups.setdefault(key, (ca_dir, {}))[1][name] = service
    failed = False
    with ExitStack() as locks:
        for _, service in services:
            locks.enter_context(service.bring_up_lock())
        for ca_dir, members in groups.values():
            try:
                _rotate_group(ca_dir, members)
            except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
                # Any backend can fail in its own way, and a failed reattach
                # ends its bring-up via die(); report it, keep going.
                _out(f"{', '.join(members)}: rotation failed: {e}")
                failed = True
    if failed:
        _out("incomplete — fix the errors above and run it again")
        return 1
    _out("done — running agents keep working on the old CA's cross-signature; "
         "new processes and bottles trust the new CA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
