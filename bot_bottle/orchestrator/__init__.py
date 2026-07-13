"""Per-host orchestrator service (PRD 0070).

A single persistent per-host service that will run the sidecar functions
(egress / git-gate / supervise), coordinate with the console, and broker
agent launches. This package is being built bottom-up, starting with the
backend-neutral "consolidation core" that needs no VM packaging:

  * `registry`      — the SQLite runtime-state store + fail-closed
                      attribution (source IP + per-bottle identity token).
  * `broker`        — the signed, structured launch-request contract + a
                      `LaunchBroker` (stub for the harness) that verifies
                      provenance before acting.
  * `sidecar`       — the consolidated per-host sidecar: a `Sidecar`
                      lifecycle contract (idempotent singleton) + a
                      `DockerSidecar` impl. One sidecar shared by all
                      bottles instead of one per bottle.
  * `service`       — the `Orchestrator`: owns the registry, brokers the
                      launch lifecycle (launch/teardown), manages the
                      shared sidecar, attributes.
  * `control_plane` — the HTTP control-plane RPC (launch / teardown /
                      list / attribute / sidecar / health).

The actual backend-native launch (a real docker/firecracker broker) and
the egress/git/supervise data plane land in later slices once this core is
proven (see the PRD sequencing: plain-process dev-harness -> docker
orchestrator -> firecracker).
"""

from __future__ import annotations

from .registry import BottleRecord, RegistryStore, new_identity_token
from .broker import (
    BrokerAuthError,
    LaunchBroker,
    LaunchRequest,
    StubBroker,
    sign_request,
    verify_request,
)
from .docker_broker import DockerBroker, DockerBrokerError
from .sidecar import DockerSidecar, Sidecar, SidecarError
from .service import Orchestrator
from .control_plane import ControlPlaneServer, dispatch, make_server

__all__ = [
    "BottleRecord",
    "RegistryStore",
    "new_identity_token",
    "BrokerAuthError",
    "LaunchBroker",
    "LaunchRequest",
    "StubBroker",
    "DockerBroker",
    "DockerBrokerError",
    "Sidecar",
    "DockerSidecar",
    "SidecarError",
    "sign_request",
    "verify_request",
    "Orchestrator",
    "ControlPlaneServer",
    "dispatch",
    "make_server",
]
