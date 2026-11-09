"""`python -m bot_bottle.control.hostctl`: run the host controller on
127.0.0.1 (`BOT_BOTTLE_HOSTCTL_PORT`, else ephemeral), publish its state file,
and serve until SIGTERM/SIGINT, plus a listener per backend for its
orchestrator (see `listeners`).
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
from ...control.lifecycle import Orchestrator
from ...lib.log import debug, info, warn
from ...paths import bot_bottle_root
from ...trust_domain import HostControlProvisioning, HostLink, signing_key
from .build import current_build_id
from .runner import HostControlState, clear_state, leave_callers_directory, write_state
from . import budget, localsock
from .github_git_tokens import GitTokenRenewer
from .audit_events import HostAudit
from .architect_bottle import ArchitectBottle, install as install_architect
from .audit_forward import AuditForwarder
from .bottles import BottleService
from .firehose import FirehoseHub
from .jobs import JobRegistry
from .listeners import GuestListeners
from .live import LiveBottles
from .remote import Supervisor
from .block_link import FEED as BLOCK_FEED
from .blocks import BlockNotifier
from .requests import RequestNotifier, orchestrator_pending
from .server import HostControlServer
from .transcript_retention import RetentionSweeper
from .sessions import SessionManager, control_socket_path, tmux_version, write_config

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


def migrate_new_host_db() -> None:
    """Create a new install's or environment's host DB tables before anything
    here writes to the file. Otherwise the first table this daemon makes (the
    forwarder's cursor) leaves a DB the CLI can't tell from an old one, and it
    asks to migrate. An existing DB is left to the CLI's migration gate."""
    # pylint: disable=import-outside-toplevel
    from ...orchestrator.store.store_manager import StoreManager
    from ...paths import host_db_path
    try:
        StoreManager(host_db_path()).migrate_if_fresh()
    except (OSError, sqlite3.Error) as e:
        warn(f"host DB not created: {e}")


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
    port = int(os.environ.get(PORT_ENV, "0") or "0")
    sessions = SessionManager({})
    migrate_new_host_db()
    # The single audit writer and the journal forwarder live as long as the
    # daemon.
    audit = HostAudit.open()
    forwarder = start_forwarder(audit)
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
    local = localsock.serve(control_socket_path(), sessions)
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
    write_state(state)
    info(f"host controller listening on {state.url} (pid {state.pid}, build {state.build_id})")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        guests.close()
        local.close()
        remote_link.close()
        requests.stop()
        blocks.stop()
        retention.stop()
        architect.stop()
        if forwarder is not None:
            forwarder.close()
        firehose.close()
        audit.close()
        clear_state(state.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
