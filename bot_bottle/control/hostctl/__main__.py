"""`python -m bot_bottle.control.hostctl`: run the host controller on
127.0.0.1 (`BOT_BOTTLE_HOSTCTL_PORT`, else ephemeral), answer on its control
socket (`runner.control_socket_path`) with that port, and serve until
SIGTERM/SIGINT, plus a listener per backend for its orchestrator (see
`listeners`).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import threading
from types import FrameType

from ...contract.auth import ROLE_GATEWAY
from ...contract.hostctl import GUEST_PORT, HOST_CONTROL, PORT_ENV, ROLE_ORCHESTRATOR
from ... import host_access
from ...control.lifecycle import Orchestrator
from ...lib.log import debug, error, info, warn
from ...paths import bot_bottle_root
from ...trust_domain import HostControlProvisioning, HostLink, signing_key
from .build import current_build_id
from .runner import (
    DaemonLockHeld, HostControlState, control_socket_path, hold_daemon_lock,
    leave_callers_directory, state_dir,
)
from . import budget, host_db, index_db, localsock
from .github_git_tokens import GitTokenRenewer
from .audit_events import HostAudit
from .architect_bottle import ArchitectBottle, install as install_architect
from .audit_forward import AuditForwarder
from .audit_retention import JournalPruner
from .bottles import BottleService
from .firehose import FirehoseHub
from .jobs import JobRegistry
from .log_rotation import LogRotator
from .listeners import GuestListeners
from .live import LiveBottles
from .remote import Supervisor
from .block_link import FEED as BLOCK_FEED
from .blocks import BlockNotifier
from .requests import RequestNotifier, orchestrator_pending
from .server import HostControlServer
from .transcript_retention import RetentionSweeper
from .sessions import SessionManager, tmux_version, write_config

# Guest listeners never accept `cli`.
GUEST_ROLES = frozenset({ROLE_ORCHESTRATOR, ROLE_GATEWAY})


def serve_running_orchestrators() -> None:
    """Listen for orchestrators an earlier daemon brought up, on their recorded
    settings."""
    # pylint: disable=import-outside-toplevel
    from ...backend import get_bottle_backend
    from ...backend.infra_service import recorded_infra_settings
    from ...backend.selection import BACKEND_NAMES
    from ...contract.hostctl import Settings

    for name in BACKEND_NAMES:
        infra = recorded_infra_settings(name)
        if infra is None:
            continue
        try:
            orchestrator = get_bottle_backend(
                Settings(infra=infra), name, prompt=False).infra_service().orchestrator()
            if orchestrator.is_running():
                orchestrator.serve_host_control()
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            debug(f"host controller: no {name} orchestrator to listen for: {e}")


def start_forwarder(audit: HostAudit) -> "AuditForwarder | None":
    """The journal forwarder, or None if it can't start."""
    # pylint: disable=import-outside-toplevel
    import time
    from ...contract.audit import utc_iso
    from ...lib.config_store import ConfigStore
    from ...paths import host_audit_dir, host_db_path, host_secrets_path
    from ...secret_source import FileSecrets
    from .audit_forward import ForwardCursors
    try:
        return AuditForwarder(
            host_audit_dir(), cursors=ForwardCursors(host_db_path()),
            config=ConfigStore(host_db_path()).audit_forward,
            secrets=FileSecrets(host_secrets_path()),
            record_failure=audit.record, now=lambda: utc_iso(time.time()),
        ).start()
    except (OSError, PermissionError, sqlite3.Error) as e:
        warn(f"audit forwarding unavailable: {e}")
        return None


def start_firehose() -> FirehoseHub:
    """The request firehose's hub, reading its sinks from the host settings."""
    # pylint: disable=import-outside-toplevel
    from ...lib.config_store import ConfigStore
    from ...paths import host_db_path, host_secrets_path
    from ...secret_source import FileSecrets
    from .firehose_sinks import SinkSet
    return FirehoseHub(settings=ConfigStore(host_db_path()).firehose,
                       sinks=SinkSet(FileSecrets(host_secrets_path()).get))


def main() -> int:
    leave_callers_directory()
    # Held for this process's life: a second host controller for this root
    # stops here, before it touches the log, the host DB or the control
    # socket (which it would otherwise unlink from under the first).
    try:
        lifetime = hold_daemon_lock()
    except DaemonLockHeld as e:
        # Nothing failed: one is running. 0, so no service manager restarts this.
        error(f"not starting: {e}")
        return 0
    # Keeps hostctl.log, which its launcher opened as stdout and stderr, from
    # growing without bound.
    log_rotator = LogRotator(state_dir() / "hostctl.log").start()
    # The host DB's only writer checks, backs up and migrates it before
    # anything here reads it, and won't start on a corrupt one.
    if not host_db.open_for_start():
        return host_db.REFUSED_EXIT_STATUS
    # Code shared with the CLI uses the stores here, not this API.
    host_access.use_stores()
    backups = host_db.DailyBackups().start()
    port = int(os.environ.get(PORT_ENV, "0") or "0")
    sessions = SessionManager({})
    # The single audit writer and the journal forwarder live as long as the
    # daemon.
    audit = HostAudit.open()
    forwarder = start_forwarder(audit)
    # Drops old journal segments past the host's retention, if one is set.
    pruner = JournalPruner(audit).start()
    # Registers the `bottle` finisher, so it must exist before adopting.
    bottles = BottleService(sessions, audit=audit)
    # The console link (ADR 0007), while remote access is enabled.
    remote_link = Supervisor(bottles)
    remote_link.start()
    # Pending requests in session status bars.
    requests = RequestNotifier(bottles.held_ids, orchestrator_pending)
    requests.start()
    # What each bottle blocked, from the audit events this daemon writes.
    blocks = BlockNotifier(bottles.held_ids)
    audit.listen(blocks.on_event)
    # And on the console's agent pages, for the shield (block_link.py).
    audit.listen(BLOCK_FEED.on_event)
    blocks.start()
    # What the control socket's `status` answers once the HTTP server is up.
    status: dict[str, object] = {}
    local = localsock.serve(control_socket_path(), sessions, lambda: dict(status) or None)
    if tmux_version() is not None:
        write_config()
        sessions.adopt()
        bottles.adopt()
    # Keeps running bottles' forwarded host logins current.
    bottles.credentials.start()
    # And the GitHub token git-gate pushes their HTTPS repos with (ADR 0014).
    GitTokenRenewer(bottles).start()
    # Measures bot-bottle's storage for the budget, off any launch's path.
    budget.METER.start()
    # The bottle architect's bottle: booted now and kept up (always on, the
    # default), else on the first suggestion.
    architect = ArchitectBottle(host_root=bot_bottle_root(), audit=audit)
    install_architect(architect)
    architect.boot_if_always_on()
    # Deletes ended sessions' transcript copies past the host's retention.
    retention = RetentionSweeper(audit)
    retention.start()
    build_id, key, jobs = current_build_id(), signing_key(HOST_CONTROL), JobRegistry()
    live, firehose = LiveBottles(), start_firehose()
    server = HostControlServer(("127.0.0.1", port), build_id, key, sessions, bottles,
                               jobs=jobs, live=live, firehose=firehose)

    def guest_server(link: HostLink) -> HostControlServer:
        return HostControlServer((link.address, GUEST_PORT), build_id, key, sessions, bottles,
                                 jobs=jobs, live=live, peers=link.network, roles=GUEST_ROLES,
                                 backend=link.backend, firehose=firehose)

    guests = GuestListeners(guest_server)
    # Orchestrators this process starts get its URL and an `orchestrator`
    # token.
    Orchestrator.host_control = HostControlProvisioning(listen=guests.listen)
    threading.Thread(target=serve_running_orchestrators, daemon=True).start()
    state = HostControlState(
        pid=os.getpid(), port=server.server_address[1], build_id=server.build_id
    )

    def _shutdown(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        # shutdown() blocks until serve_forever returns, so not from its
        # thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    status.update(pid=state.pid, port=state.port, build_id=state.build_id)
    info(f"host controller listening on {state.url} (pid {state.pid}, build {state.build_id})")
    # The index DB is derived: a missing or damaged one is rebuilt from the
    # journal. Checked once `status` answers, so a large one doesn't hold up
    # the start; a prune and a query wait for the check.
    index_db.open_in_background()
    try:
        server.serve_forever()
    finally:
        status.clear()
        server.server_close()
        guests.close()
        local.close()
        remote_link.close()
        requests.stop()
        blocks.stop()
        retention.stop()
        pruner.stop()
        backups.stop()
        architect.stop()
        if forwarder is not None:
            forwarder.close()
        firehose.close()
        audit.close()
        log_rotator.stop()
        os.close(lifetime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
