"""Per-host orchestrator service (PRD 0070).

A single persistent per-host service that will run the sidecar functions
(egress / git-gate / supervise), coordinate with the console, and broker
agent launches. This package is being built bottom-up, starting with the
backend-neutral "consolidation core" that needs no VM packaging:

  * `registry`      — the SQLite runtime-state store + fail-closed
                      attribution (source IP + per-bottle identity token).
  * `control_plane` — the HTTP control-plane RPC (register / deregister /
                      list / attribute / health), with live reload.

Launch/teardown, the launch broker, and the actual egress/git/supervise
data plane land in later slices once this core is proven (see the PRD's
sequencing: plain-process dev-harness -> docker orchestrator -> firecracker).
"""

from __future__ import annotations

from .registry import BottleRecord, RegistryStore, new_identity_token
from .control_plane import ControlPlaneServer, dispatch, make_server

__all__ = [
    "BottleRecord",
    "RegistryStore",
    "new_identity_token",
    "ControlPlaneServer",
    "dispatch",
    "make_server",
]
