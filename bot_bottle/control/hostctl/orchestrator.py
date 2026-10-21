"""Orchestrator bring-up through the host controller (PRD 0092).

`ensure` and `status` run inside the daemon; `ensure_via_daemon` is the CLI
side, which starts the daemon if needed, submits the job and streams its output.
"""

from __future__ import annotations

import dataclasses

from ...contract.hostctl import ORCHESTRATOR_PATH, Settings
from ...lib import log


def _service(settings: Settings):  # type: ignore[no-untyped-def]
    from ...backend import get_bottle_backend  # pylint: disable=import-outside-toplevel

    backend = get_bottle_backend(settings, settings.operation.backend or None, prompt=False)
    return backend.name, backend.infra_service()


def ensure(settings: Settings, *, restart: bool = False) -> dict[str, object]:
    """Bring the backend's orchestrator and gateway up (daemon side). With
    `restart`, stop them first."""
    name, service = _service(settings)
    if restart:
        log.info(f"stopping the {name} orchestrator and gateway")
        service.stop()
    url = service.ensure_running()
    # One it adopted (e.g. from before a daemon restart) still needs a listener.
    service.orchestrator().serve_host_control()
    return {"backend": name, "url": url}


def status(settings: Settings) -> dict[str, object]:
    """Whether the backend's control plane answers, and where (daemon side)."""
    name, service = _service(settings)
    healthy = service.is_healthy()
    return {"backend": name, "running": healthy, "url": service.url() if healthy else ""}


def ensure_via_daemon(settings: Settings, backend: str, *, restart: bool = False) -> str:
    """Ask the host controller to bring `backend`'s orchestrator up, print its
    output as it runs, and return the control-plane URL. Dies on failure, or
    if the user detaches before it finishes (the job keeps running)."""
    # pylint: disable=import-outside-toplevel
    from . import runner
    from .client import HostControlClient

    request = dataclasses.replace(
        settings, operation=dataclasses.replace(settings.operation, backend=backend),
    )
    client = HostControlClient(runner.ensure_running(), runner.cli_token())
    action = "restart" if restart else "ensure"
    job_id = str(client.post(f"{ORCHESTRATOR_PATH}/{action}", {"settings": request.to_json()})["job"])
    final = client.follow(job_id, log.write_line)
    if final is None:
        log.die(f"detached; the bring-up continues as job {job_id} (see `hostctl jobs`)")
    if final.get("type") == "error":
        log.die(str(final.get("message", "orchestrator bring-up failed")))
    value = final.get("value")
    if not isinstance(value, dict) or not isinstance(value.get("url"), str):
        log.die("host controller returned no orchestrator URL")
    return value["url"]


__all__ = ["ensure", "ensure_via_daemon", "status"]
