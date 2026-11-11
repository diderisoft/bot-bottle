"""Rotate every backend's gateway CA.

    python -m bot_bottle.control.rotate_ca

It runs in the host controller (`POST /orchestrator/rotate-ca`, a job whose
lines this prints): restarting a gateway is a bring-up, and bring-up records
infrastructure state, which only the host controller writes (ADR 0020).

Under each backend's bring-up lock, backends are grouped by shared CA dir
(docker and macOS share one). A group with a running gateway rolls over to a
new CA cross-signed by the current one, then restarts and reattaches, so agents
holding the old CA keep working. A group with no running gateway has its CA
cleared. A failed group keeps its CA and the exit is non-zero.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from .. import host_access
from ..contract.hostctl import ORCHESTRATOR_PATH
from ..lib import log
from .gateway import GatewayError, rotate_gateway_ca

if TYPE_CHECKING:
    from ..backend.infra_service import InfraService
    from ..contract.hostctl import Settings


def _out(msg: str) -> None:
    log.write_line(f"rotate-ca: {msg}")


def _services(settings: "Settings") -> list[tuple[str, Callable[[], "InfraService"]]]:
    """(backend name, infra service factory) for every available backend."""
    from ..backend import get_bottle_backend, has_backend, known_backend_names
    return [
        (name, lambda name=name: get_bottle_backend(settings, name).infra_service())
        for name in sorted(known_backend_names())
        if has_backend(settings, name)
    ]


def _rotate_group(ca_dir: Path | None, members: dict[str, "InfraService"]) -> None:
    """Rotate one group's CA. Raises on failure."""
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
    # The rollover runs the gateway's installed code, so update it first.
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


def rotate(settings: "Settings") -> dict[str, object]:
    """Rotate every available backend's gateway CA (host-controller side).
    `{"rotated": False}` when a group failed; each failure is a line."""
    services = [(name, factory()) for name, factory in _services(settings)]
    if not services:
        _out("no backend available on this host; nothing to rotate")
        return {"rotated": True}
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
                # Report a backend's failure and keep going.
                _out(f"{', '.join(members)}: rotation failed: {e}")
                failed = True
    if failed:
        _out("incomplete — fix the errors above and run it again")
        return {"rotated": False}
    _out("done — running agents keep working on the old CA's cross-signature; "
         "new processes and bottles trust the new CA")
    return {"rotated": True}


def main(argv: list[str] | None = None) -> int:
    """Have the host controller rotate, printing its lines. 0 when every
    group rotated."""
    del argv  # no flags
    from ..cli.settings import settings_from_env  # pylint: disable=import-outside-toplevel
    client = host_access.client()
    body: dict[str, object] = {"settings": settings_from_env().to_json()}
    job_id = str(client.post(f"{ORCHESTRATOR_PATH}/rotate-ca", body)["job"])
    final = client.follow(job_id, print)
    if final is None:
        _out(f"detached; the rotation continues as job {job_id} (see `bot-bottle jobs`)")
        return 1
    if final.get("type") == "error":
        _out(f"rotation failed: {final.get('message', '')}")
        return 1
    value = final.get("value")
    return 0 if isinstance(value, dict) and value.get("rotated") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
