"""Host-controller listeners where each backend's orchestrator guest reaches
the host: the docker publish bridge, the macOS control network's vmnet gateway,
the firecracker orchestrator TAP. Never `0.0.0.0`. Each accepts only peers on
its link and only guest roles.
"""

from __future__ import annotations

import errno
import ipaddress
import threading
import time
from collections.abc import Callable

from ...lib.log import debug, info, warn
from ...trust_domain import HostLink
from .server import HostControlServer

# Retry window for an address not up yet (a vmnet bridge appears with its first
# container).
RETRY_SECONDS = 120.0
_RETRY_POLL_SECONDS = 0.5


class GuestListeners:
    """One `HostControlServer` per host link, built by `make_server`."""

    def __init__(self, make_server: Callable[[HostLink], HostControlServer], *,
                 retry_seconds: float = RETRY_SECONDS) -> None:
        self._make_server = make_server
        self._retry_seconds = retry_seconds
        self._servers: dict[str, HostControlServer] = {}
        self._pending: dict[str, HostLink] = {}
        self._lock = threading.Lock()
        self._closed = False

    def listen(self, link: HostLink) -> None:
        """Serve `link` (idempotent). An address not up yet is retried in the
        background; other bind failures are logged."""
        with self._lock:
            if self._closed:
                return
            current = self._servers.get(link.address)
            if current is not None and current.peers == ipaddress.ip_network(link.network):
                return
            if link.address in self._pending:
                self._pending[link.address] = link
                return
            if current is not None:
                self._stop(self._servers.pop(link.address))
            try:
                self._start(link)
            except OSError as e:
                if e.errno != errno.EADDRNOTAVAIL:
                    warn(f"host controller: can't listen on {link.address}: {e}")
                    return
                self._pending[link.address] = link
                threading.Thread(target=self._retry, args=(link.address,), daemon=True,
                                 name=f"hostctl-listen-{link.address}").start()

    def addresses(self) -> list[str]:
        """The addresses being served."""
        with self._lock:
            return sorted(self._servers)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for server in self._servers.values():
                self._stop(server)
            self._servers.clear()

    def _start(self, link: HostLink) -> None:
        server = self._make_server(link)
        self._servers[link.address] = server
        threading.Thread(target=server.serve_forever, daemon=True,
                         name=f"hostctl-{link.address}").start()
        host, port = server.server_address[:2]
        info(f"host controller listening on {host}:{port} for {link.network}")

    @staticmethod
    def _stop(server: HostControlServer) -> None:
        server.shutdown()
        server.server_close()

    def _retry(self, address: str) -> None:
        deadline = time.monotonic() + self._retry_seconds
        while time.monotonic() < deadline:
            time.sleep(_RETRY_POLL_SECONDS)
            with self._lock:
                link = self._pending.get(address)
                if self._closed or link is None:
                    return
                try:
                    self._start(link)
                except OSError as e:
                    debug(f"host controller: {address} not up yet: {e}")
                    continue
                del self._pending[address]
                return
        with self._lock:
            self._pending.pop(address, None)
        warn(f"host controller: {address} never came up; orchestrators there can't reach it")


__all__ = ["GuestListeners", "RETRY_SECONDS"]
