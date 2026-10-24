"""Bottles held by the host controller: launch, list, teardown.

A launch job prepares the bottle without prompting, then hands it to a
holder thread that stays inside `backend.launch(plan)` for the bottle's
life. The holder runs the agent in a tmux session and, when the agent exits,
leaves the launch context (tearing the bottle down) and settles its state.
The `bottle` session finisher hands the agent's exit to the holder; a session
re-adopted after a daemon restart has no holder, so its bottle is left for
`bot-bottle cleanup`.
"""

from __future__ import annotations

import contextvars
import dataclasses
import queue
import shlex
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...bottle_state import capture_claude_session_state, settle_state
from ...contract.auth import ROLE_CLI
from ...contract.hostctl import CONFIRM_ERROR, InfraSettings, LaunchRequest, Settings, exit_code
from ...lib import log
from ...paths import host_secrets_path
from ...util import slugify
from .audit_events import HostAudit, digest_manifest, digest_request
from .sessions import ExitStatus, SessionManager, SessionRecord

if TYPE_CHECKING:
    from ...backend import BottleBackend, BottlePlan
    from ...manifest import ManifestIndex

SESSION_KIND = "bottle"
# How long a stopped agent gets to exit before its bottle is finished anyway.
STOP_GRACE_SECONDS = 15.0
# How long to wait for that report once the agent's session is gone.
GONE_REPORT_SECONDS = 2.0


class ConfirmationNeeded(Exception):
    """The launch stopped for a yes/no; resending with `assume_yes` goes ahead."""

    job_error_code = CONFIRM_ERROR


@dataclass
class _Held:
    id: str
    backend: str
    exited: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    exit_status: ExitStatus | None = None
    stop_requested: bool = False


class PromptUnsupported(ValueError):
    """The agent CLI can't start an interactive session with a first message."""


def agent_args(template: str, startup_args: tuple[str, ...], prompt: str,
               headless: bool = True, *, remote_view: bool = False) -> list[str]:
    """The agent CLI's arguments: the provider's bypass flags, its startup
    args (and its `remote_view_args` for a session the console launched),
    then the initial task when there is one: run to completion when
    `headless`, else as the first message of an interactive session."""
    # pylint: disable=import-outside-toplevel
    from ...agent_provider import get_provider, runtime_for

    args = [*runtime_for(template).bypass_args, *startup_args]
    if remote_view:
        args.extend(get_provider(template).remote_view_args())
    if prompt and headless:
        args.extend(get_provider(template).headless_prompt(prompt))
    elif prompt:
        interactive = get_provider(template).interactive_prompt(prompt)
        if interactive is None:
            raise PromptUnsupported(
                f"the {template} agent can't start an interactive session with a prompt")
        args.extend(interactive)
    return args


def _default_backend(settings: Settings) -> "BottleBackend[Any, Any]":
    from ...backend import get_bottle_backend  # pylint: disable=import-outside-toplevel
    return get_bottle_backend(settings, prompt=False)


def _recorded_infra(backend: str) -> InfraSettings | None:
    from ...backend.infra_service import recorded_infra_settings  # pylint: disable=import-outside-toplevel
    return recorded_infra_settings(backend)


def with_recorded_infra(settings: Settings, backend: str,
                        recorded: Callable[[str], InfraSettings | None] = _recorded_infra,
                        ) -> Settings:
    """`settings` for `backend` with the infra settings its last bring-up
    recorded (defaults when none): only `cli` may set those."""
    return dataclasses.replace(
        settings,
        operation=dataclasses.replace(settings.operation, backend=backend),
        infra=recorded(backend) or InfraSettings(),
    )


def _default_manifest(cwd: str) -> "ManifestIndex":
    from ...manifest import ManifestIndex  # pylint: disable=import-outside-toplevel
    return ManifestIndex.resolve(cwd)


def _log_preflight(plan: "BottlePlan", backend_name: str) -> None:
    from ...manifest.preview import manifest_to_yaml  # pylint: disable=import-outside-toplevel
    log.write_line("")
    log.write_line(f"backend: {backend_name}")
    for line in manifest_to_yaml(plan.manifest).splitlines():
        log.write_line(line)


def _save_work(bottle: Any, plan: "BottlePlan", identity: str) -> None:
    """Before teardown: save what the agent changed in a copied git
    workspace as a patch, so its work outlives the bottle."""
    from ...workspace import export_patch_script, save_patch  # pylint: disable=import-outside-toplevel
    try:
        workspace = plan.workspace_plan
        if not (workspace.enabled and workspace.has_host_git_dir) or workspace.mount:
            return
        result = bottle.exec(export_patch_script(shlex.quote(workspace.guest_path)))
    except Exception as e:  # pylint: disable=broad-exception-caught  # never block teardown
        log.warn(f"bottle {identity}: couldn't save the agent's changes: {e}")
        return
    if result.returncode != 0:
        log.warn(f"bottle {identity}: couldn't save the agent's changes: "
                 f"{result.stderr.strip()[:200] or 'git diff failed'}")
        return
    path = save_patch(identity, result.stdout)
    if path is not None:
        log.info(f"saved the agent's changes to {path}")


def _stderr_line(line: str) -> None:
    marker = log.parse_stage(line)
    sys.stderr.write((f"bot-bottle: {marker[1]}" if marker else line) + "\n")


class BottleService:
    """The daemon's bottles. Registers the `bottle` finisher on `sessions`."""

    def __init__(
        self,
        sessions: SessionManager,
        *,
        backend_for: Callable[[Settings], "BottleBackend[Any, Any]"] = _default_backend,
        manifest_for: Callable[[str], "ManifestIndex"] = _default_manifest,
        secrets_file: Path | None = None,
        recorded_infra: Callable[[str], InfraSettings | None] = _recorded_infra,
        audit: HostAudit | None = None,
    ) -> None:
        self.audit = audit if audit is not None else HostAudit()
        self._sessions = sessions
        self._backend_for = backend_for
        self._manifest_for = manifest_for
        self._recorded_infra = recorded_infra
        self._secrets_file = secrets_file
        self._held: dict[str, _Held] = {}
        self._lock = threading.Lock()
        sessions.register(SESSION_KIND, self._finish)

    # -- launch --------------------------------------------------------------

    @property
    def sessions(self) -> SessionManager:
        """The tmux sessions the agents run in (the console link relays them)."""
        return self._sessions

    def launch(self, request: LaunchRequest, role: str = ROLE_CLI) -> dict[str, object]:
        """A `bottle.launch` job: prepare without prompting, hand the bottle
        to a holder, and return once its agent runs in a tmux session. A
        non-`cli` request runs on the backend's recorded infra settings."""
        digest = digest_request(request.to_json())
        try:
            result = self._launch(request, role)
        except ConfirmationNeeded:
            raise  # nothing happened yet; the caller is asked first
        except BaseException as e:
            self.audit.brokered("broker_launch", "bottle.launch", role=role, success=False,
                                request_digest=digest, bottle=request.identity or None,
                                detail=str(e)[:200])
            raise
        self.audit.brokered("broker_launch", "bottle.launch", role=role, success=True,
                            request_digest=digest, bottle=str(result.get("bottle", "")) or None)
        return result

    def _launch(self, request: LaunchRequest, role: str) -> dict[str, object]:
        # pylint: disable=import-outside-toplevel
        from ...backend import BottleSpec
        from ...secret_source import ChainSecrets, FileSecrets, MappingSecrets
        from .remote import ROLE_REMOTE

        wanted = request.identity or (slugify(request.label) if request.label else "")
        if wanted and self._running(wanted):
            log.die(f"bottle {wanted} is already running")
        backend = self._backend_for(request.settings)
        if role != ROLE_CLI:
            request = dataclasses.replace(request, settings=with_recorded_infra(
                request.settings, backend.name, self._recorded_infra))
            backend = self._backend_for(request.settings)
        cwd = request.cwd or str(Path.home())
        index = self._manifest_for(cwd)
        index.require_agent(request.agent)
        spec = BottleSpec(
            manifest=index, agent_name=request.agent, copy_cwd=request.copy_cwd,
            mount_cwd=request.mount_cwd,
            user_cwd=cwd, identity=request.identity, label=request.label,
            color=request.color, bottle_names=request.bottles, headless=True,
            image_policy=request.image_policy,
            host_keys=tuple(sorted(request.host_keys.items())),
        )
        secrets = ChainSecrets(MappingSecrets(request.secrets),
                               FileSecrets(self._secrets_file or host_secrets_path()))
        stage_dir = Path(tempfile.mkdtemp(prefix="bot-bottle-stage."))
        identity = ""
        handed_off = False
        try:
            log.stage("prepare", "Reading the bottle")
            plan = backend.prepare(spec, stage_dir=stage_dir, secrets=secrets)
            identity = str(getattr(plan, "slug", ""))
            if request.headless or not request.assume_yes:
                _log_preflight(plan, backend.name)
            self._confirm(backend, plan, request)
            if self._running(identity):
                log.die(f"bottle {identity} is already running")
            ready: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
            threading.Thread(
                target=contextvars.Context().run,
                args=(self._hold, backend, plan, request),
                kwargs={"identity": identity, "stage_dir": stage_dir,
                        "job_sink": log.current_sink(), "ready": ready,
                        "remote_view": role == ROLE_REMOTE},
                daemon=True, name=f"hostctl-bottle-{identity}",
            ).start()
            handed_off = True
            kind, value = ready.get()
            if kind == "error":
                raise value
            return value
        finally:
            if not handed_off:
                settle_state(identity)
                shutil.rmtree(stage_dir, ignore_errors=True)

    @staticmethod
    def _confirm(backend: "BottleBackend[Any, Any]", plan: "BottlePlan",
                 request: LaunchRequest) -> None:
        """Stop for a yes/no unless the request already gave it. A stale
        image fails a headless launch outright, as it always has."""
        from ...image_cache import StaleImageError  # pylint: disable=import-outside-toplevel
        try:
            backend.prelaunch_checks(plan)
        except StaleImageError as e:
            if request.headless:
                log.die(str(e))
            if not request.assume_yes:
                raise ConfirmationNeeded(f"{e}\nLaunch anyway?") from None
            log.warn(str(e))  # launching anyway; say why it might be off
        if not request.assume_yes:
            raise ConfirmationNeeded("launch this agent?")

    def _hold(self, backend: "BottleBackend[Any, Any]", plan: "BottlePlan",
              request: LaunchRequest, *, identity: str, stage_dir: Path,
              job_sink: Callable[[str], None], ready: "queue.Queue[tuple[str, Any]]",
              remote_view: bool = False) -> None:
        """The holder thread: launch, run the agent, wait, tear down."""
        is_ready = threading.Event()
        held = _Held(identity, backend.name)
        template = str(getattr(plan, "agent_provider_template", "claude"))
        policy = digest_manifest(plan.manifest)

        def sink(line: str) -> None:
            # Into the launch job until it has returned, then the daemon's log.
            (_stderr_line if is_ready.is_set() else job_sink)(line)

        error: BaseException | None = None
        with log.redirect(sink):
            try:
                with backend.launch(plan) as bottle:
                    # The orchestrator's registry id names this run everywhere
                    # in the journal; a backend that didn't register gets one.
                    activation = bottle.registry_id or str(uuid.uuid4())
                    command, env = bottle.agent_command(agent_args(
                        template, plan.agent_provision.startup_args, request.prompt,
                        headless=request.headless, remote_view=remote_view))
                    with self._lock:
                        self._held[identity] = held
                    self._sessions.start(SessionRecord(
                        id=identity, kind=SESSION_KIND,
                        meta={"backend": backend.name, "agent": request.agent,
                              "label": request.label, "provider": template,
                              "activation": activation, "manifest_digest": policy},
                        writable_allowed=plan.manifest.agent.remote_writable,
                    ), command, env)
                    log.info(f"agent running in session {identity}")
                    self.audit.bottled_agent_start(
                        bottle=identity, activation=activation, manifest_digest=policy,
                        backend=backend.name, agent=request.agent)
                    ready.put(("ready", {"bottle": identity, "session": identity,
                                         "activation": activation}))
                    is_ready.set()
                    held.exited.wait()
                    code = exit_code(held.exit_status.status, held.exit_status.signal) \
                        if held.exit_status else 0
                    self.audit.bottled_agent_exit(
                        bottle=identity, activation=activation, manifest_digest=policy,
                        exit_code=code, stop_requested=held.stop_requested)
                    log.info(f"session {identity} ended (exit {code}); "
                             f"removing {bottle.name}")
                    _save_work(bottle, plan, identity)
                    if template == "claude":
                        capture_claude_session_state(identity, code)
            except BaseException as e:  # pylint: disable=broad-exception-caught
                if is_ready.is_set():
                    log.warn(f"bottle {identity}: teardown failed: {e}")
                else:
                    error = e
            finally:
                with self._lock:
                    self._held.pop(identity, None)
                settle_state(identity)
                shutil.rmtree(stage_dir, ignore_errors=True)
                held.done.set()
                if error is not None:
                    ready.put(("error", error))

    def _finish(self, record: SessionRecord, exit_status: ExitStatus) -> None:
        """The `bottle` finisher: hand the exit to the holder and wait for its
        teardown. With no holder (re-adopted after a restart) there's no
        launch context to leave, so the bottle is left for `cleanup`."""
        with self._lock:
            held = self._held.get(record.id)
        if held is not None:
            held.exit_status = exit_status
            held.exited.set()
            held.done.wait()
            return
        code = exit_code(exit_status.status, exit_status.signal)
        self.audit.bottled_agent_exit(
            bottle=record.id, activation=str(record.meta.get("activation", "")),
            manifest_digest=str(record.meta.get("manifest_digest", "")),
            exit_code=code, stop_requested=False)
        log.warn(f"bottle {record.id} ({record.meta.get('backend', '?')}): its agent exited "
                 f"(exit {code}) with nothing holding the bottle; run `bot-bottle cleanup` "
                 f"to remove it")
        if record.meta.get("provider") == "claude":
            capture_claude_session_state(record.id, code)
        settle_state(record.id)

    # -- list / teardown -----------------------------------------------------

    def held_ids(self) -> list[str]:
        """The sessions of the bottles this daemon holds."""
        with self._lock:
            return list(self._held)

    def _running(self, bottle_id: str) -> bool:
        with self._lock:
            if bottle_id in self._held:
                return True
        return self._sessions.record(bottle_id) is not None and self._sessions.exists(bottle_id)

    def session_state(self, session_id: str) -> dict[str, object]:
        """`GET /sessions/{id}`: whether the agent runs, and how it exited."""
        status = self._sessions.exit_status(session_id)
        return {
            "running": status is None and self._sessions.exists(session_id),
            "exit": None if status is None else {"status": status.status, "signal": status.signal},
        }

    def list(self) -> list[dict[str, object]]:
        """`GET /bottles`: the bottles whose sessions this daemon knows."""
        with self._lock:
            held = set(self._held)
        return [
            {"id": r.id, "backend": r.meta.get("backend", ""), "agent": r.meta.get("agent", ""),
             "label": r.meta.get("label", ""), "held": r.id in held,
             "session": self.session_state(r.id)}
            for r in self._sessions.records() if r.kind == SESSION_KIND
        ]

    def known(self, bottle_id: str) -> bool:
        record = self._sessions.record(bottle_id)
        return record is not None and record.kind == SESSION_KIND

    def _await_report(self, bottle_id: str, reported: Callable[[float], bool]) -> bool:
        """Wait for a stopped agent's exit to be reported: up to
        `STOP_GRACE_SECONDS`, but only `GONE_REPORT_SECONDS` once its session
        is gone (a launcher killed before it could report never will)."""
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        gone_at: float | None = None
        while not reported(0.2):
            now = time.monotonic()
            if gone_at is None and not self._sessions.exists(bottle_id):
                gone_at = now
            if now > deadline or (gone_at is not None and now - gone_at > GONE_REPORT_SECONDS):
                return False
        return True

    def teardown(self, bottle_id: str, role: str = ROLE_CLI) -> dict[str, object]:
        """A `bottle.teardown` job: stop the agent, then wait until its bottle
        is torn down. An agent that ignores the hangup is finished anyway."""
        digest = digest_request({"bottle": bottle_id})
        try:
            result = self._teardown(bottle_id)
        except BaseException as e:
            self.audit.brokered("broker_teardown", "bottle.teardown", role=role, success=False,
                                request_digest=digest, bottle=bottle_id, detail=str(e)[:200])
            raise
        self.audit.brokered("broker_teardown", "bottle.teardown", role=role, success=True,
                            request_digest=digest, bottle=bottle_id)
        return result

    def _teardown(self, bottle_id: str) -> dict[str, object]:
        with self._lock:
            held = self._held.get(bottle_id)
        if held is None and not self.known(bottle_id):
            log.die(f"no bottle {bottle_id}")
        if held is not None:
            held.stop_requested = True
        log.info(f"stopping the agent in {bottle_id}")
        self._sessions.kill(bottle_id)
        forced = ExitStatus(0, "SIGHUP")
        reported: Callable[[float], bool] = (
            held.exited.wait if held is not None
            else lambda t: self._sessions.wait_exit(bottle_id, t) is not None)
        if not self._await_report(bottle_id, reported):
            log.warn(f"{bottle_id}: the agent didn't report its exit; tearing down anyway")
            threading.Thread(target=self._sessions.died, args=(bottle_id, forced),
                             daemon=True).start()
        if held is not None:
            held.done.wait()
        status = self._sessions.wait_exit(bottle_id, timeout=STOP_GRACE_SECONDS) or forced
        log.info(f"{bottle_id} torn down")
        return {"bottle": bottle_id, "exit": {"status": status.status, "signal": status.signal}}


__all__ = ["BottleService", "ConfirmationNeeded", "SESSION_KIND", "agent_args",
           "with_recorded_infra"]
