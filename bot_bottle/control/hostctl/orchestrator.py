"""Orchestrator bring-up through the host controller: `ensure` and `status` run
in the daemon; `ensure_via_daemon` is the CLI side.
"""

from __future__ import annotations

import dataclasses

from ...contract.hostctl import ORCHESTRATOR_PATH, Settings
from ...lib import log


def _service(settings: Settings):  # type: ignore[no-untyped-def]
    from ...backend import get_bottle_backend  # pylint: disable=import-outside-toplevel

    backend = get_bottle_backend(settings, settings.operation.backend or None, prompt=False)
    return backend.name, backend.infra_service()


def ensure(settings: Settings, *, restart: bool = False,
           allow_reattach_failures: bool = False) -> dict[str, object]:
    """Bring the backend's infra pair up (daemon side) for `bot-bottle up`;
    `restart` stops it first. Builds locally only when `settings` asks
    (`--build`), never on an earlier bring-up's say-so.
    `allow_reattach_failures` names bottles that won't reattach instead of
    failing (`--allow-reattach-failures`)."""
    # pylint: disable-next=import-outside-toplevel
    from ...backend.gateway_attach import AttachPolicy

    name, service = _service(settings)
    if restart:
        # Before stopping anything: a restart that can't come back up is worse
        # than none.
        service.require_infra_images(inherit_build=False)
        # Images first, while the pair still serves: a build mustn't be
        # downtime for the running bottles.
        service.prepare_images()
        log.info(f"stopping the {name} orchestrator and gateway")
        service.stop()
    policy = AttachPolicy.REPORT if allow_reattach_failures else AttachPolicy.STRICT
    try:
        url = service.ensure_running(inherit_build=False, attach_policy=policy)
    finally:
        # An adopted one still needs a listener, and so does one whose
        # bring-up then failed (a bottle that won't reattach): host control,
        # and the console with it, stay up to deal with it.
        _serve_host_control(service)
    return {"backend": name, "url": url}


def _serve_host_control(service) -> None:  # type: ignore[no-untyped-def]
    """Listen for the orchestrator's host-control calls if it's running."""
    try:
        orchestrator = service.orchestrator()
        if orchestrator.is_running():
            orchestrator.serve_host_control()
    except Exception as e:  # pylint: disable=broad-exception-caught  # never mask the bring-up's
        log.warn(f"host control not served: {e}")


def status(settings: Settings) -> dict[str, object]:
    """Whether the backend's control plane answers, and where."""
    name, service = _service(settings)
    healthy = service.is_healthy()
    return {"backend": name, "running": healthy, "url": service.url() if healthy else ""}


def ensure_via_daemon(settings: Settings, backend: str, *, restart: bool = False,
                      allow_reattach_failures: bool = False) -> str:
    """Have the host controller bring `backend`'s orchestrator up, print its
    output, and return the control-plane URL. Dies on failure or detach (the
    job keeps running)."""
    # pylint: disable=import-outside-toplevel
    from . import runner
    from .client import HostControlClient

    request = dataclasses.replace(
        settings, operation=dataclasses.replace(settings.operation, backend=backend),
    )
    client = HostControlClient(runner.ensure_running(), runner.cli_token())
    action = "restart" if restart else "ensure"
    body: dict[str, object] = {"settings": request.to_json()}
    if allow_reattach_failures:
        body["allow_reattach_failures"] = True
    job_id = str(client.post(f"{ORCHESTRATOR_PATH}/{action}", body)["job"])
    shown: list[str] = []

    def write(line: str) -> None:
        shown.append(line)
        log.write_line(line)
    final = client.follow(job_id, write)
    if final is None:
        log.die(f"detached; the bring-up continues as job {job_id} (see `bot-bottle jobs`)")
    if final.get("type") == "error":
        message = str(final.get("message", "orchestrator bring-up failed"))
        # A `die` in the job already wrote its message as a log line.
        if any(message.partition("\n")[0] in line for line in shown[-3:]):
            raise log.Die(1, message)
        log.die(message)
    value = final.get("value")
    if not isinstance(value, dict) or not isinstance(value.get("url"), str):
        log.die("host controller returned no orchestrator URL")
    return value["url"]


__all__ = ["ensure", "ensure_via_daemon", "status"]
