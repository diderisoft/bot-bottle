"""Bottles held by the host controller: launch, list, teardown.

A launch job prepares without prompting, then hands off to a holder thread that
stays inside `backend.launch(plan)` for the bottle's life, runs the agent in
tmux, and tears down when it exits. A session re-adopted after a daemon restart
has no holder, so its bottle is left for `cleanup bots`.
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

from ...bottle_state import capture_session_state, is_preserved, settle_state
from ...contract.auth import ROLE_CLI
from ...contract.hostctl import (CONFIRM_ERROR, ORIGIN_CONSOLE, ORIGIN_TERMINAL, InfraSettings,
                                 LaunchRequest, Settings, exit_code)
from ...lib import log
from ...manifest.schema import DEFAULT_BOTTLE
from ...paths import host_db_path, host_secrets_path
from ... import bottle_state, run_bottle, run_manifest
from ...backend import adoption
from ...backend.bottle_size import memory_bytes, recorded_memory
from ...lib.config_store import ConfigStore, HostBudget
from . import bottle_metrics, budget, relabel
from .build import RESTART_NEEDED
from .audit_events import HostAudit, digest_manifest, digest_request
from .credential_refresh import CredentialRefresher
from .sessions import ExitStatus, SessionManager, SessionRecord
from .transcripts import TranscriptFollower, close_stale
from .transcript_serve import masking_plan as masking

if TYPE_CHECKING:
    from ...backend import Bottle, BottleBackend, BottlePlan
    from ...manifest import ManifestIndex

SESSION_KIND = "bottle"
# How long a stopped agent gets before its bottle is finished anyway.
STOP_GRACE_SECONDS = 15.0
# Exporting the workspace's changes before teardown gives up after this long.
PATCH_TIMEOUT_SECONDS = 300.0
# How long to wait for its report once its session is gone.
GONE_REPORT_SECONDS = 2.0


class ConfirmationNeeded(Exception):
    """The launch stopped for a yes/no; resend with `assume_yes`."""

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


def agent_args(harness: str, startup_args: tuple[str, ...], prompt: str,
               headless: bool = True, *, resume: bool = False) -> list[str]:
    """The agent CLI's args: with `resume` (its home kept from its last run)
    the harness's continue args, then bypass flags, startup args, then the
    initial task, headless or as the first message. A console launch runs the
    harness's own TUI, input box and all, like a terminal one."""
    # pylint: disable=import-outside-toplevel
    from ...agent_provider import get_provider, runtime_for

    args = [*(get_provider(harness).continue_args() if resume else ()),
            *runtime_for(harness).bypass_args, *startup_args]
    if prompt and headless:
        args.extend(get_provider(harness).headless_prompt(prompt))
    elif prompt:
        interactive = get_provider(harness).interactive_prompt(prompt)
        if interactive is None:
            raise PromptUnsupported(
                f"the {harness} agent can't start an interactive session with a prompt")
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
    """`settings` for `backend` with its last recorded infra settings, which
    only `cli` may set."""
    return dataclasses.replace(
        settings,
        operation=dataclasses.replace(settings.operation, backend=backend),
        infra=recorded(backend) or InfraSettings(),
    )


def _push_credential(registry_id: str, slot: str, value: str, env_var_secret: str) -> None:
    """Give the orchestrator that registered `registry_id` (the environment's)
    a refreshed egress token. Raises `OrchestratorClientError` naming it when
    it's missing, unreachable, rejects, or doesn't know the bottle."""
    # pylint: disable=import-outside-toplevel
    from ..client import for_bottle
    for_bottle(lambda client: client.update_agent_secret(registry_id, slot, value,
                                                         env_var_secret),
               what=f"bottle {registry_id}")


def _chain(request: LaunchRequest) -> tuple[str, ...]:
    """The launch's bottle chain: the bottles picked, else `DEFAULT_BOTTLE`."""
    return tuple(request.bottles) or (DEFAULT_BOTTLE,)


def _default_manifest(cwd: str) -> "ManifestIndex":
    from ...manifest import ManifestIndex  # pylint: disable=import-outside-toplevel
    return ManifestIndex.resolve(cwd)


def _log_preflight(plan: "BottlePlan", backend_name: str) -> None:
    from ...manifest.preview import manifest_to_yaml  # pylint: disable=import-outside-toplevel
    log.write_line("")
    log.write_line(f"backend: {backend_name}")
    for line in manifest_to_yaml(plan.manifest).splitlines():
        log.write_line(line)
    from ...backend import launch_isolation_note  # pylint: disable=import-outside-toplevel
    note = launch_isolation_note(plan, backend_name)
    if note:
        log.warn(note)


def _code_changed() -> bool:
    """Whether the code on disk differs from what this process started from:
    a launch would mix the two through lazy imports."""
    from .build import code_changed  # pylint: disable=import-outside-toplevel
    return code_changed()


def _save_work(bottle: Any, workspace: Any, identity: str) -> None:
    """Before teardown, save a copied git workspace's changes as a patch.
    `workspace` is the plan's workspace or an adoption record's."""
    from ...workspace import export_patch_script, save_patch  # pylint: disable=import-outside-toplevel
    try:
        if not (workspace.enabled and workspace.has_host_git_dir):
            return
        result = bottle.exec(export_patch_script(shlex.quote(workspace.guest_path)),
                             timeout=PATCH_TIMEOUT_SECONDS)
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


# How a warning line reads in the launch log.
_WARNING = "bot-bottle: warning:"


def _stderr_line(line: str) -> None:
    marker = log.parse_stage(line)
    sys.stderr.write((f"bot-bottle: {marker[1]}" if marker else line) + "\n")


class BottleService:
    """The daemon's bottles; registers the `bottle` finisher."""

    def __init__(
        self,
        sessions: SessionManager,
        *,
        backend_for: Callable[[Settings], "BottleBackend[Any, Any]"] = _default_backend,
        manifest_for: Callable[[str], "ManifestIndex"] = _default_manifest,
        secrets_file: Path | None = None,
        recorded_infra: Callable[[str], InfraSettings | None] = _recorded_infra,
        audit: HostAudit | None = None,
        code_changed: Callable[[], bool] = _code_changed,
        host_budget: Callable[[], HostBudget] = budget.host_budget,
        storage: Callable[[], dict[str, int] | None] = budget.METER.current,
    ) -> None:
        self.audit = audit if audit is not None else HostAudit()
        self._code_changed = code_changed
        self._sessions = sessions
        self._backend_for = backend_for
        self._manifest_for = manifest_for
        self._recorded_infra = recorded_infra
        self._secrets_file = secrets_file
        self._held: dict[str, _Held] = {}
        self._host_budget = host_budget
        self._storage = storage
        # Admitted against the budget but not held yet: id -> label.
        self._launching: dict[str, tuple[str, int]] = {}  # label, memory
        self._admitting = threading.Lock()
        # What adopted bottles hold until they end (e.g. a Firecracker slot).
        self._adopted: dict[str, Callable[[], None]] = {}
        # Holderless (re-adopted) bottles asked to stop: audited as a stop, not a crash.
        self._stop_requested: set[str] = set()
        # Each running session's transcript follower (PRD 0095).
        self._followers: dict[str, TranscriptFollower] = {}
        # Keeps forwarded host logins current; started by the daemon.
        self.credentials = CredentialRefresher(_push_credential, audit=self.audit)
        self._lock = threading.Lock()
        sessions.register(SESSION_KIND, self._finish)

    # --- launch ---

    @property
    def sessions(self) -> SessionManager:
        """The tmux sessions agents run in."""
        return self._sessions

    def launch(self, request: LaunchRequest, role: str = ROLE_CLI) -> dict[str, object]:
        """A `bottle.launch` job: prepare without prompting, hand off to a
        holder, and return once the agent runs in tmux. Non-`cli` requests use
        the recorded infra settings."""
        digest = digest_request(request.to_json())
        try:
            result = self._launch(request, role)
        except ConfirmationNeeded:
            raise  # nothing happened yet
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
        from ...secret_source import LaunchSecrets, MappingSecrets, host_store
        from ...manifest.versions import launch_versions
        from .remote import ROLE_REMOTE

        if self._code_changed():
            log.die(f"bot-bottle was updated since the host controller started: {RESTART_NEEDED}")
        if request.identity and self._running(request.identity):
            log.die(f"bottle {request.identity} is already running")
        from_console = role == ROLE_REMOTE
        if not from_console and request.identity and not request.agent_id:
            # A resume of a bottle the console started comes back as that
            # console agent, from wherever it's asked for. The id is the one
            # this bottle's own state recorded, never the request's.
            carried = bottle_state.console_agent_id(request.identity)
            if carried:
                request = dataclasses.replace(request, agent_id=carried)
                from_console = True
        backend = self._backend_for(request.settings)
        if role != ROLE_CLI:
            request = dataclasses.replace(request, settings=with_recorded_infra(
                request.settings, backend.name, self._recorded_infra))
            backend = self._backend_for(request.settings)
        cwd = request.cwd or str(Path.home())
        # Hand edits become a version before the launch reads them (PRD 0103).
        versions = run_bottle.with_content(launch_versions(cwd), request.run_bottle)
        index = run_bottle.index_for(self._manifest_for(cwd), text=request.run_bottle,
                                     identity=request.identity)
        index.require_agent(request.agent)
        spec = BottleSpec(
            manifest=index, agent_name=request.agent, copy_cwd=request.copy_cwd,
            user_cwd=cwd, identity=request.identity, label=request.label,
            color=request.color, bottle_names=request.bottles, headless=True,
            image_policy=request.image_policy,
            host_keys=tuple(sorted(request.host_keys.items())),
            fresh_guest=request.fresh_guest,
            default_audit_delivery=ConfigStore(host_db_path()).audit_delivery(),
        )
        secrets = LaunchSecrets(MappingSecrets(request.secrets), host_store(
            self._secrets_file or host_secrets_path()), request.answers)
        stage_dir = Path(tempfile.mkdtemp(prefix="bot-bottle-stage."))
        identity = ""
        handed_off = False
        try:
            log.stage("prepare", "Reading the bottle")
            plan = backend.prepare(spec, stage_dir=stage_dir, secrets=secrets)
            identity = str(getattr(plan, "slug", ""))
            run_bottle.keep(identity, index)
            if request.headless:
                _log_preflight(plan, backend.name)
            self._confirm(backend, plan, request)
            if self._running(identity):
                log.die(f"bottle {identity} is already running")
            self._admit(identity, request.label or identity, memory_bytes(plan.manifest.bottle))
            ready: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
            threading.Thread(
                target=contextvars.Context().run,
                args=(self._hold, backend, plan, request),
                kwargs={"identity": identity, "stage_dir": stage_dir, "versions": versions,
                        "job_sink": log.current_sink(), "ready": ready,
                        "from_console": from_console},
                daemon=True, name=f"hostctl-bottle-{identity}",
            ).start()
            handed_off = True
            kind, value = ready.get()
            if kind == "error":
                raise value
            return value
        finally:
            with self._lock:
                self._launching.pop(identity, None)
            if not handed_off and not self._running(identity):  # never a running one's
                settle_state(identity)
                shutil.rmtree(stage_dir, ignore_errors=True)

    def _admit(self, identity: str, label: str, memory: int) -> None:
        """Refuse a launch the host budget can't fit, else count it as
        running until its holder takes it. One admission at a time, so two
        launches can't both take the last room."""
        with self._admitting:
            limits, storage = self._host_budget(), self._storage()
            refused = budget.refusal(label, self.allocation(), limits, storage, memory)
            if refused is not None:
                log.die(refused)
            warning = budget.storage_warning(limits, storage)
            if warning is not None:
                log.warn(warning)
            with self._lock:
                self._launching[identity] = (label, memory)

    def allocation(self) -> list[dict[str, object]]:
        """The bottles memory is allocated to: held, adopted, launching, or
        with a running session; each at the memory it was given."""
        with self._lock:
            taken = set(self._held) | set(self._adopted)
            launching = dict(self._launching)
        out: list[dict[str, object]] = []
        labels = {m.identity: m.label for m in bottle_state.all_metadata()}
        for r in self._sessions.records():
            if r.kind != SESSION_KIND:
                continue
            if r.id in taken or r.id in launching or self.session_state(r.id)["running"]:
                entry: dict[str, object] = {"id": r.id, "label": labels.get(r.id) or r.id,
                                            "memory": recorded_memory(r.meta)}
                metrics = bottle_metrics.collect(r.id, str(r.meta.get("backend") or ""))
                if metrics is not None:
                    entry["cpu_pct"] = metrics.cpu_pct
                    entry["mem_used"] = metrics.mem_used
                out.append(entry)
                launching.pop(r.id, None)
        out.extend({"id": i, "label": label, "memory": memory}
                   for i, (label, memory) in launching.items())
        return out

    def live_metrics(self, identity: str) -> dict[str, object] | None:
        """A bottle's CPU % and memory in use for the console's live gauge
        (`live_metrics`), on its own CPU baseline; None if it can't be read."""
        record = self._sessions.record(identity)
        if record is None:
            return None
        metrics = bottle_metrics.collect(identity, str(record.meta.get("backend") or ""),
                                         sample="live")
        if metrics is None:
            return None
        return {"id": identity, "cpu_pct": metrics.cpu_pct, "mem_used": metrics.mem_used,
                "memory": recorded_memory(record.meta)}

    @staticmethod
    def _confirm(backend: "BottleBackend[Any, Any]", plan: "BottlePlan",
                 request: LaunchRequest) -> None:
        """The pre-launch checks. A launch doesn't ask to go ahead; only a stale
        image stops for a yes/no (unless the request gave it), and fails a
        headless launch."""
        from ...image_cache import StaleImageError  # pylint: disable=import-outside-toplevel
        try:
            backend.prelaunch_checks(plan)
        except StaleImageError as e:
            if request.headless:
                log.die(str(e))
            if not request.assume_yes:
                raise ConfirmationNeeded(f"{e}\nLaunch anyway?") from None
            log.warn(str(e))  # launching anyway

    def _hold(self, backend: "BottleBackend[Any, Any]", plan: "BottlePlan",
              request: LaunchRequest, *, identity: str, stage_dir: Path,
              versions: dict[str, object],
              job_sink: Callable[[str], None], ready: "queue.Queue[tuple[str, Any]]",
              from_console: bool = False) -> None:
        """The holder thread: launch, run the agent, wait, tear down."""
        is_ready = threading.Event()
        held = _Held(identity, backend.name)
        harness = str(getattr(plan, "harness", "claude"))
        policy = digest_manifest(plan.manifest)

        warnings: list[str] = []  # the launch's, for the session's status bar

        def sink(line: str) -> None:
            # Into the launch job until it returns, then the daemon's log.
            if not is_ready.is_set() and line.startswith(_WARNING):
                warnings.append(line[len(_WARNING):].strip())
            (_stderr_line if is_ready.is_set() else job_sink)(line)

        error: BaseException | None = None
        with log.redirect(sink):
            try:
                with backend.launch(plan) as bottle, masking(plan):
                    # The registry id names this run in the journal.
                    activation = bottle.registry_id or str(uuid.uuid4())
                    # Onto its kept home, the agent picks up its last conversation.
                    continuing = getattr(bottle, "home_kept", False) is True
                    command, env = bottle.agent_command(agent_args(
                        harness, plan.agent_provision.startup_args, request.prompt,
                        headless=request.headless, resume=continuing))
                    with self._lock:
                        self._held[identity] = held
                    self._follow(plan, bottle, request, identity=identity, activation=activation,
                                 continuing=continuing)
                    # Lets a restarted controller finish this bottle.
                    record = adoption.record_for(plan, bottle)
                    if not adoption.write(identity, record):
                        log.warn(f"bottle {identity}: has no record to keep how to finish it "
                                 "after a host controller restart")
                    run_manifest.record_launch(
                        identity, plan.manifest.bottle, names=_chain(request),
                        cwd=request.cwd, host_version=str(versions.get("host", "")))
                    if record.credentials is not None:
                        self.credentials.add(
                            identity, record.credentials, registry_id=bottle.registry_id,
                            bottle=bottle, env_var_secret=bottle.env_var_secret,
                            current=plan.agent_provision.provisioned_env)
                    self._sessions.start(SessionRecord(
                        id=identity, kind=SESSION_KIND,
                        meta={"backend": backend.name, "agent": request.agent,
                              "provider": harness,
                              "activation": activation, "manifest_digest": policy,
                              "memory": memory_bytes(plan.manifest.bottle),
                              "origin": ORIGIN_CONSOLE if from_console else ORIGIN_TERMINAL,
                              # The console's id for an agent it launched, so a
                              # restarted host controller can pick it up again.
                              **({"agent_id": request.agent_id} if request.agent_id else {})},
                        writable_allowed=plan.manifest.agent.remote_writable,
                    ), command, env, warnings)
                    bottle_state.record_origin(
                        identity, ORIGIN_CONSOLE if from_console else ORIGIN_TERMINAL,
                        request.agent_id if from_console else "")
                    log.info(f"agent running in session {identity}")
                    self.audit.bottled_agent_start(
                        bottle=identity, activation=activation, manifest_digest=policy,
                        backend=backend.name, agent=request.agent, bottle_versions=versions)
                    ready.put(("ready", {"bottle": identity, "session": identity,
                                         "activation": activation}))
                    is_ready.set()
                    held.exited.wait()
                    self.credentials.remove(identity)
                    code = exit_code(held.exit_status.status, held.exit_status.signal) \
                        if held.exit_status else 0
                    self.audit.bottled_agent_exit(
                        bottle=identity, activation=activation, manifest_digest=policy,
                        exit_code=code, stop_requested=held.stop_requested)
                    log.info(f"session {identity} ended (exit {code}); "
                             f"removing {bottle.name}")
                    self._unfollow(identity, bottle)
                    _save_work(bottle, plan.workspace_plan, identity)
                    capture_session_state(identity, code)
            except BaseException as e:  # pylint: disable=broad-exception-caught
                if is_ready.is_set():
                    log.warn(f"bottle {identity}: teardown failed: {e}")
                else:
                    error = e
            finally:
                self._unfollow(identity)  # if teardown failed before it
                with self._lock:
                    self._held.pop(identity, None)
                self._settle(identity, backend)
                shutil.rmtree(stage_dir, ignore_errors=True)
                held.done.set()
                if error is not None:
                    ready.put(("error", error))

    def adopt(self) -> None:
        """After a daemon start, once sessions are adopted: re-take what each
        running bottle needs held in this process, before anything launches."""
        for record in self._sessions.records():
            name = str(record.meta.get("backend", ""))
            if record.kind != SESSION_KIND or not name or not self._sessions.exists(record.id):
                continue
            try:
                backend = self._backend_for(
                    with_recorded_infra(Settings(), name, self._recorded_infra))
                release = backend.hold_adopted(record.id)
            except Exception as e:  # pylint: disable=broad-exception-caught  # one bad record mustn't stop the daemon
                log.warn(f"bottle {record.id}: couldn't adopt its {name} resources: {e}")
                continue
            if release is not None:
                with self._lock:
                    self._adopted[record.id] = release
            self._refollow(backend, record)
            self._recredential(backend, record)
        close_stale({r.id for r in self._sessions.records()}, self.audit)

    # --- transcripts ---

    def _follow(self, plan: "BottlePlan", bottle: "Bottle", request: LaunchRequest, *,
                identity: str, activation: str, continuing: bool = False) -> None:
        """Start copying the session's transcript, if its harness keeps one;
        `continuing` its last conversation, from where that left off."""
        # pylint: disable=import-outside-toplevel
        from ...agent_provider import get_provider
        from .remote import terminal_agent_id
        harness = str(getattr(plan, "harness", "claude"))
        try:
            glob = get_provider(harness).transcript_glob(plan)
            if glob is None:
                return
            follower = TranscriptFollower.begin(
                bottle, session=identity, activation=activation, glob=glob,
                agent_id=request.agent_id or terminal_agent_id(identity, activation),
                audit=self.audit, harness=harness, continuing=continuing)
        except Exception as e:  # pylint: disable=broad-exception-caught  # the agent runs regardless
            # Not a launch warning: the agent runs as usual, without a copy.
            log.info(f"bottle {identity}: not copying its transcript: {e}")
            return
        follower.start(bottle)
        with self._lock:
            self._followers[identity] = follower

    def refresh_credentials(self, identity: str) -> dict[str, object]:
        """Refresh a running bottle's forwarded host login now."""
        return self.credentials.refresh_now(identity)

    def backend_for_session(self, identity: str) -> "BottleBackend[Any, Any] | None":
        """The backend a running bottle is on, or None if it isn't known."""
        record = self._sessions.record(identity)
        name = str(record.meta.get("backend", "")) if record is not None else ""
        if not name:
            return None
        return self._backend_for(with_recorded_infra(Settings(), name, self._recorded_infra))

    def grant_repo(self, identity: str, grant: str) -> dict[str, object]:
        """Grant a repo to a running bottle (`repo_grants.grant_repo`)."""
        # pylint: disable-next=import-outside-toplevel
        from .repo_grants import grant_repo
        record = self._sessions.record(identity)
        name = str(record.meta.get("backend", "")) if record is not None else ""
        if not name:
            return {"error": "that bottle isn't running on this host"}
        backend = self._backend_for(with_recorded_infra(Settings(), name, self._recorded_infra))
        return grant_repo(backend, identity, grant,
                          secrets_file=self._secrets_file or host_secrets_path())

    def _recredential(self, backend: "BottleBackend[Any, Any]", record: SessionRecord) -> None:
        """Keep a re-adopted bottle's forwarded host login current."""
        adopted = adoption.read(record.id)
        if adopted is None or adopted.credentials is None:
            return
        try:
            bottle = backend.attach_adopted(record.id, adopted)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"bottle {record.id}: can't refresh its host login: {e}")
            return
        if bottle is not None:
            self.credentials.add(record.id, adopted.credentials,
                                 registry_id=adopted.registry_id, bottle=bottle)

    def _refollow(self, backend: "BottleBackend[Any, Any]", record: SessionRecord) -> None:
        """Resume a re-adopted session's transcript copy from its offsets."""
        try:
            follower = TranscriptFollower.resume(
                session=record.id, activation=str(record.meta.get("activation", "")),
                audit=self.audit)
            adopted = adoption.read(record.id)
            bottle = backend.attach_adopted(record.id, adopted) if adopted else None
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"bottle {record.id}: couldn't resume copying its transcript: {e}")
            return
        if follower is None:
            return
        if bottle is None:
            follower.close()
            return
        follower.start(bottle)
        with self._lock:
            self._followers[record.id] = follower

    def _unfollow(self, identity: str, bottle: "Bottle | None" = None,
                  activation: str = "") -> None:
        """After the agent exits: read the transcript once more and close the
        copy. Never raises."""
        with self._lock:
            follower = self._followers.pop(identity, None)
        try:
            if follower is None and activation:
                follower = TranscriptFollower.resume(session=identity, activation=activation,
                                                     audit=self.audit)
            if follower is not None:
                follower.close(bottle)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"bottle {identity}: couldn't close its transcript copy: {e}")

    def _finish(self, record: SessionRecord, exit_status: ExitStatus) -> None:
        """The `bottle` finisher: hand the exit to the holder and wait for
        teardown. Without a holder (re-adopted) the bottle is left for
        `cleanup bots`."""
        with self._lock:
            held = self._held.get(record.id)
        if held is not None:
            held.exit_status = exit_status
            held.exited.set()
            held.done.wait()
            return
        with self._lock:
            requested = record.id in self._stop_requested
            self._stop_requested.discard(record.id)
        code = exit_code(exit_status.status, exit_status.signal)
        self.audit.bottled_agent_exit(
            bottle=record.id, activation=str(record.meta.get("activation", "")),
            manifest_digest=str(record.meta.get("manifest_digest", "")),
            exit_code=code, stop_requested=requested)
        capture_session_state(record.id, code)
        try:
            self._finish_adopted(record, code)
        finally:
            with self._lock:
                release = self._adopted.pop(record.id, None)
            if release is not None:
                release()
        name = str(record.meta.get("backend", ""))
        self._settle(record.id, self._backend_for(
            with_recorded_infra(Settings(), name, self._recorded_infra)) if name else None)

    def _settle(self, identity: str, backend: "BottleBackend[Any, Any] | None") -> None:
        """After teardown: keep the state and the agent's home for `resume`,
        or remove both."""
        settle_state(identity)
        remove_home = getattr(backend, "remove_home", None)
        if remove_home is None or not identity or is_preserved(identity):
            return
        try:
            remove_home(identity)
        except Exception as e:  # pylint: disable=broad-exception-caught  # cleanup finds it later
            log.warn(f"bottle {identity}: couldn't remove its home: {e}; "
                     "`bot-bottle cleanup bots` will")

    def _finish_adopted(self, record: SessionRecord, code: int) -> None:
        """Tear down a bottle a previous controller launched, from the record
        it left: save the work, remove the bottle, revoke its deploy keys."""
        # pylint: disable=import-outside-toplevel
        from ...git_gate.provision import live_grant_refs, revoke_deploy_keys
        from ...bottle_state import git_gate_state_dir
        from ...secret_source import FileSecrets

        self.credentials.remove(record.id)
        name = str(record.meta.get("backend", ""))
        adopted = adoption.read(record.id)
        if adopted is None or not name:
            self._unfollow(record.id, None, str(record.meta.get("activation", "")))
            log.warn(f"bottle {record.id} ({name or '?'}): its agent exited (exit {code}) "
                     f"with nothing holding the bottle; run `bot-bottle cleanup bots` to remove it")
            return
        log.info(f"session {record.id} ended (exit {code}); removing {adopted.container}")
        try:
            backend = self._backend_for(
                with_recorded_infra(Settings(), name, self._recorded_infra))
            with backend.adopted_bottle(record.id, adopted) as bottle:
                self._unfollow(record.id, bottle, str(record.meta.get("activation", "")))
                _save_work(bottle, adopted.workspace, record.id)
        except Exception as e:  # pylint: disable=broad-exception-caught  # still revoke keys
            log.warn(f"bottle {record.id}: teardown failed: {e}; run `bot-bottle cleanup bots`")
        try:
            state = git_gate_state_dir(record.id)
            revoke_deploy_keys(adopted.deploy_keys + live_grant_refs(record.id), state,
                               FileSecrets(self._secrets_file or host_secrets_path()))
        except Exception as e:  # pylint: disable=broad-exception-caught  # report, don't crash
            log.warn(f"bottle {record.id}: couldn't revoke its deploy keys: {e}; "
                     f"revoke them by hand")

    # --- list / teardown ---

    def snapshot(self, identity: str, *, seq: int | None = None) -> dict[str, object]:
        """Snapshot a running bottle's workspace (`snapshots.take`) and audit
        it. SnapshotUnavailable when there's nothing to take."""
        # pylint: disable=import-outside-toplevel
        from .snapshots import SnapshotUnavailable, take
        attached = self._attached(identity)
        if isinstance(attached, str):
            raise SnapshotUnavailable(attached)
        bottle, record, adopted = attached
        activation = str(record.meta.get("activation", ""))
        taken = take(bottle, adopted.workspace, session=identity, activation=activation, seq=seq)
        self.audit.snapshot_taken(bottle=identity, activation=activation, snapshot=taken)
        return taken

    def rename(self, identity: str, label: str) -> dict[str, object]:
        """A new label for the bottle `identity` (`relabel.rename`)."""
        return relabel.rename(identity, label)

    def read_workspace_image(self, identity: str, path: str) -> dict[str, object]:
        """An image file from a running bottle (`workspace_files.serve_read`)."""
        from .workspace_files import serve_read  # pylint: disable=import-outside-toplevel
        return serve_read(self._attached, identity, path)

    def write_workspace_file(self, identity: str, path: str,
                             data: bytes) -> dict[str, object]:
        """Write to a running bottle's workspace (`workspace_files.serve_write`)."""
        from .workspace_files import serve_write  # pylint: disable=import-outside-toplevel
        return serve_write(self._attached, identity, path, data)

    def _attached(self, identity: str) -> "tuple[Bottle, Any, Any] | str":
        """A running bottle reached through its backend, with its session
        record and adoption, or why it can't be."""
        record = self._sessions.record(identity)
        adopted = adoption.read(identity)
        if record is None or adopted is None or not self._running(identity):
            return f"bottle {identity} isn't running"
        name = str(record.meta.get("backend", ""))
        backend = self._backend_for(with_recorded_infra(Settings(), name, self._recorded_infra))
        bottle = backend.attach_adopted(identity, adopted)
        if bottle is None:
            return f"the {name} backend can't reach bottle {identity}"
        return bottle, record, adopted

    attached = _attached  # for the console's chunked uploads (`uploads`)

    def held_ids(self) -> list[str]:
        """Sessions of the bottles this daemon holds."""
        with self._lock:
            return list(self._held)

    def _running(self, bottle_id: str) -> bool:
        with self._lock:
            if bottle_id in self._held:
                return True
        return self._sessions.record(bottle_id) is not None and self._sessions.exists(bottle_id)

    def wake_transcript(self, bottle_id: str) -> None:
        """The agent was typed into: read its transcript now (a quiet one
        is polled less often)."""
        with self._lock:
            follower = self._followers.get(bottle_id)
        if follower is not None:
            follower.wake()

    def session_state(self, session_id: str) -> dict[str, object]:
        """`GET /sessions/{id}`: whether the agent runs, and how it exited."""
        status = self._sessions.exit_status(session_id)
        return {
            "running": status is None and self._sessions.exists(session_id),
            "exit": None if status is None else {
                "status": status.status, "signal": status.signal,
                "last_output": self._sessions.last_output(session_id)},
        }

    def list(self) -> list[dict[str, object]]:
        """`GET /bottles`: the bottles whose sessions this daemon knows."""
        with self._lock:
            held = set(self._held)
        # The label and parent are the bottled agent's, kept only there.
        recorded = {m.identity: m for m in bottle_state.all_metadata()}
        return [
            {"id": r.id, "backend": r.meta.get("backend", ""), "agent": r.meta.get("agent", ""),
             "label": m.label if (m := recorded.get(r.id)) else "",
             "parent": m.parent if m else "", "activation": r.meta.get("activation", ""),
             "origin": r.meta["origin"], "writable": r.writable_allowed,
             "agent_id": r.meta.get("agent_id", ""),
             "held": r.id in held, "session": self.session_state(r.id)}
            for r in self._sessions.records() if r.kind == SESSION_KIND
        ]

    def known(self, bottle_id: str) -> bool:
        record = self._sessions.record(bottle_id)
        return record is not None and record.kind == SESSION_KIND

    def _await_report(self, bottle_id: str, reported: Callable[[float], bool]) -> bool:
        """Wait for a stopped agent's exit report: up to `STOP_GRACE_SECONDS`,
        or `GONE_REPORT_SECONDS` once its session is gone."""
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
        """A `bottle.teardown` job: stop the agent and wait for teardown. An
        agent ignoring the hangup is finished anyway."""
        digest = digest_request({"bottle": bottle_id})
        try:
            result = self._teardown(bottle_id)
        except BaseException as e:
            with self._lock:
                self._stop_requested.discard(bottle_id)
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
        else:
            with self._lock:
                self._stop_requested.add(bottle_id)
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
