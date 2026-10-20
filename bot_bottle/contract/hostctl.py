"""The host controller's wire contract (PRD 0092): routes, response shapes,
its trust domain and the `Settings` each request carries, shared by the daemon
and its clients."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .auth import ROLE_CLI, ROLE_GATEWAY, TrustDomain

SERVICE_NAME = "bot-bottle-hostctl"

# Port the daemon binds; unset means an ephemeral port, published in its state file.
PORT_ENV = "BOT_BOTTLE_HOSTCTL_PORT"

STATUS_PATH = "/status"

# Callers authenticate with `Authorization: Bearer <token>`.
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"

ROLE_ORCHESTRATOR = "orchestrator"

# The host controller's own key, which the orchestrator never holds: it gets an
# `orchestrator` token instead, so it can't mint `cli` tokens and restart itself.
HOST_CONTROL = TrustDomain(
    name="host-control",
    key_filename="hostctl-token",
    roles=frozenset({ROLE_CLI, ROLE_ORCHESTRATOR, ROLE_GATEWAY}),
    key_env="BOT_BOTTLE_HOSTCTL_KEY",
    token_env="BOT_BOTTLE_HOSTCTL_JWT",
)


DEFAULT_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:latest"
DEFAULT_GATEWAY_IMAGE = "bot-bottle-gateway:latest"
DEFAULT_EGRESS_PORT = 9099
DEFAULT_CLEANUP_COMMAND_TIMEOUT_SECONDS = 120.0

_SectionT = TypeVar("_SectionT", "OperationSettings", "InfraSettings")


@dataclass(frozen=True)
class OperationSettings:
    """Knobs that apply to one request only; `cli` and `orchestrator` may set them."""

    # Empty means auto-select.
    backend: str = ""
    no_cache: bool = False
    cleanup_command_timeout_seconds: float = DEFAULT_CLEANUP_COMMAND_TIMEOUT_SECONDS


@dataclass(frozen=True)
class InfraSettings:
    """Knobs that describe the host infra itself; only `cli` may set them.

    An empty string or 0 means the built-in default."""

    # Build the infra images locally instead of pulling the published ones.
    infra_build_local: bool = False
    # `docker`, `podman`, `vm`, or empty to pick the first usable engine.
    build_engine: str = ""
    artifact_cache: str = ""
    orchestrator_image: str = DEFAULT_ORCHESTRATOR_IMAGE
    gateway_image: str = DEFAULT_GATEWAY_IMAGE
    egress_port: int = DEFAULT_EGRESS_PORT
    docker_gateway_subnet: str = ""
    docker_ca_mount: str = ""
    docker_root_mount: str = ""
    docker_client_network: str = ""
    docker_host_address: str = ""
    fc_kernel: str = ""
    fc_dropbear: str = ""
    fc_pool_size: int = 0
    fc_ip_base: str = ""
    fc_iface_prefix: str = ""
    fc_nft_table: str = ""
    fc_orch_iface: str = ""
    fc_gw_iface: str = ""
    macos_container_dns: str = ""

    def fingerprint(self) -> str:
        """sha256 of the canonical JSON: equal settings, equal fingerprint."""
        canonical = json.dumps(_section_json(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    """Everything bring-up and launch would otherwise read from the environment."""

    operation: OperationSettings = field(default_factory=OperationSettings)
    infra: InfraSettings = field(default_factory=InfraSettings)

    def to_json(self) -> dict[str, dict[str, object]]:
        return {"operation": _section_json(self.operation), "infra": _section_json(self.infra)}

    @classmethod
    def from_json(cls, data: object) -> "Settings":
        """Parse a `to_json` value. Missing keys take their defaults; unknown
        keys and wrongly typed values raise ValueError."""
        body = _mapping(data, "settings")
        _reject_unknown(body, {"operation", "infra"}, "settings")
        return cls(
            operation=_section_from_json(OperationSettings, body.get("operation", {}), "operation"),
            infra=_section_from_json(InfraSettings, body.get("infra", {}), "infra"),
        )


def _section_json(section: OperationSettings | InfraSettings) -> dict[str, object]:
    return dataclasses.asdict(section)


def _mapping(data: object, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or not all(isinstance(k, str) for k in data):
        raise ValueError(f"{where}: expected an object")
    return data


def _reject_unknown(body: Mapping[str, Any], known: set[str], where: str) -> None:
    unknown = sorted(set(body) - known)
    if unknown:
        raise ValueError(f"{where}: unknown keys {', '.join(unknown)}")


def _section_from_json(cls: type[_SectionT], data: object, where: str) -> _SectionT:
    body = _mapping(data, where)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    _reject_unknown(body, set(fields), where)
    values: dict[str, object] = {}
    for name, value in body.items():
        # Every field's default is a plain value of its type; bool is not an int here.
        expected = type(fields[name].default)
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected) or (expected is not bool and isinstance(value, bool)):
            raise ValueError(f"{where}.{name}: expected {expected.__name__}, "
                             f"got {type(value).__name__}")
        if expected in (int, float) and value < 0:
            raise ValueError(f"{where}.{name}: must not be negative")
        values[name] = value
    return cls(**values)  # type: ignore[arg-type]


def status_body(*, build_id: str, pid: int) -> dict[str, object]:
    """`GET /status` response: which code the daemon runs and its pid."""
    return {"service": SERVICE_NAME, "build_id": build_id, "pid": pid}


def bearer(token: str) -> dict[str, str]:
    """The request header carrying `token`."""
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"}


__all__ = [
    "AUTH_HEADER", "AUTH_SCHEME", "DEFAULT_CLEANUP_COMMAND_TIMEOUT_SECONDS",
    "DEFAULT_EGRESS_PORT", "DEFAULT_GATEWAY_IMAGE", "DEFAULT_ORCHESTRATOR_IMAGE",
    "HOST_CONTROL", "InfraSettings", "OperationSettings", "PORT_ENV", "ROLE_ORCHESTRATOR",
    "SERVICE_NAME", "STATUS_PATH", "Settings", "bearer", "status_body",
]
