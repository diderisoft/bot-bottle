"""The host controller's wire contract (PRD 0092): routes, response shapes,
its trust domain and the `Settings` each request carries, shared by the daemon
and its clients."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .auth import ROLE_CLI, ROLE_GATEWAY, TrustDomain

SERVICE_NAME = "bot-bottle-hostctl"

# Port the daemon binds; unset means an ephemeral port, published in its state file.
PORT_ENV = "BOT_BOTTLE_HOSTCTL_PORT"

STATUS_PATH = "/status"
# GET /jobs, /jobs/{id}, /jobs/{id}/events (newline-delimited JSON, streamed).
JOBS_PATH = "/jobs"
# POST /orchestrator/{ensure,restart} start jobs; POST /orchestrator/status answers.
# Each takes {"settings": Settings.to_json()}.
ORCHESTRATOR_PATH = "/orchestrator"
# GET /sessions/{id}: {"running": bool, "exit": {"status", "signal"} | null}.
# GET /sessions/{id}/attach upgrades to the attach WebSocket (see control/hostctl/attach).
SESSIONS_PATH = "/sessions"
# POST /bottles (a `LaunchRequest`) starts a `bottle.launch` job whose result is
# {"bottle": id, "session": id}; GET /bottles lists the bottles the daemon holds;
# DELETE /bottles/{id} starts a `bottle.teardown` job.
BOTTLES_PATH = "/bottles"

# A job `error` event with this `code` stopped for a yes/no the caller can give:
# resend the request with `assume_yes` to go ahead.
CONFIRM_ERROR = "confirm"

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


BOTTLE_COLORS = ("red", "green", "yellow", "blue", "magenta")
IMAGE_POLICIES = ("fresh", "cached")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MAP_FIELDS = ("secrets", "host_keys")


@dataclass(frozen=True)
class LaunchRequest:
    """`POST /bottles`: a fully resolved, non-interactive launch.

    `cwd`, `copy_cwd`, `secrets` and `host_keys` are `cli`-only: a path on
    the host, and values the CLI resolved or its user confirmed."""

    agent: str
    bottles: tuple[str, ...] = ()
    label: str = ""
    color: str = ""
    # The headless initial task; empty for an interactive session.
    prompt: str = ""
    headless: bool = False
    # Resume this bottle identity; empty mints a fresh one.
    identity: str = ""
    image_policy: str = "fresh"
    # Go ahead without the yes/no a `CONFIRM_ERROR` would ask for.
    assume_yes: bool = False
    settings: Settings = field(default_factory=Settings)
    cwd: str = ""
    copy_cwd: bool = False
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    # Repo name -> the upstream host key the user confirmed for this launch.
    host_keys: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        body = dataclasses.asdict(self)
        body["bottles"] = list(self.bottles)
        body["settings"] = self.settings.to_json()
        return body

    @classmethod
    def from_json(cls, data: object, role: str) -> "LaunchRequest":
        """Parse and validate a request from `role`. ValueError for a
        malformed one; PermissionError when a non-`cli` role sets a
        `cli`-only field."""
        body = _mapping(data, "launch")
        names = {f.name for f in dataclasses.fields(cls)}
        _reject_unknown(body, names, "launch")
        forbidden = sorted(set(body) & set(LAUNCH_CLI_ONLY))
        if role != ROLE_CLI and forbidden:
            raise PermissionError(f"launch: {', '.join(forbidden)} only for the cli role")
        values: dict[str, Any] = {}
        for name in sorted((names - {"bottles", "settings", *_MAP_FIELDS}) & set(body)):
            expected = type(getattr(_DEFAULT_LAUNCH, name))
            # Exact types: a bool is not a str here, nor an int a bool.
            if not isinstance(body[name], expected) or \
                    isinstance(body[name], bool) != (expected is bool):
                raise ValueError(f"launch.{name}: expected {expected.__name__}")
            values[name] = body[name]
        if "bottles" in body:
            bottles = body["bottles"]
            if not isinstance(bottles, list) or not all(isinstance(b, str) for b in bottles):
                raise ValueError("launch.bottles: expected a list of names")
            values["bottles"] = tuple(bottles)
        for name in set(_MAP_FIELDS) & set(body):
            values[name] = _str_map(body[name], f"launch.{name}")
        if "settings" in body:
            values["settings"] = Settings.from_json(body["settings"])
        if "agent" not in values:
            raise ValueError("launch.agent: required")
        request = cls(**values)
        request.check()
        return request

    def check(self) -> None:
        """ValueError unless every field holds an allowed value."""
        for name in (self.agent, *self.bottles):
            if not _NAME.match(name):
                raise ValueError(f"launch: invalid agent or bottle name {name!r}")
        if self.identity and not _IDENTITY.match(self.identity):
            raise ValueError(f"launch.identity: invalid identity {self.identity!r}")
        if self.color and self.color not in BOTTLE_COLORS:
            raise ValueError(f"launch.color: one of {', '.join(BOTTLE_COLORS)}")
        if self.image_policy not in IMAGE_POLICIES:
            raise ValueError(f"launch.image_policy: one of {', '.join(IMAGE_POLICIES)}")
        if self.cwd and not self.cwd.startswith("/"):
            raise ValueError("launch.cwd: expected an absolute path")
        if self.copy_cwd and not self.cwd:
            raise ValueError("launch.copy_cwd needs cwd")
        if self.headless and not self.prompt:
            raise ValueError("launch: headless needs a prompt")


LAUNCH_CLI_ONLY = ("cwd", "copy_cwd", "secrets", "host_keys")
_DEFAULT_LAUNCH = LaunchRequest(agent="")


def _str_map(data: object, where: str) -> dict[str, str]:
    body = _mapping(data, where)
    if not all(isinstance(v, str) for v in body.values()):
        raise ValueError(f"{where}: values must be strings")
    return dict(body)


def exit_code(status: int, signal_name: str = "") -> int:
    """A shell-style exit code for an agent that exited with `status`, or
    was killed by `signal_name` (128 + its number)."""
    if signal_name:
        try:
            return 128 + int(signal.Signals[signal_name])
        except KeyError:
            return 128
    return status


def status_body(*, build_id: str, pid: int) -> dict[str, object]:
    """`GET /status` response: which code the daemon runs and its pid."""
    return {"service": SERVICE_NAME, "build_id": build_id, "pid": pid}


def bearer(token: str) -> dict[str, str]:
    """The request header carrying `token`."""
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"}


__all__ = [
    "AUTH_HEADER", "AUTH_SCHEME", "BOTTLES_PATH", "BOTTLE_COLORS", "CONFIRM_ERROR",
    "DEFAULT_CLEANUP_COMMAND_TIMEOUT_SECONDS", "IMAGE_POLICIES", "LAUNCH_CLI_ONLY",
    "LaunchRequest", "exit_code",
    "DEFAULT_EGRESS_PORT", "DEFAULT_GATEWAY_IMAGE", "DEFAULT_ORCHESTRATOR_IMAGE",
    "HOST_CONTROL", "InfraSettings", "JOBS_PATH", "ORCHESTRATOR_PATH", "OperationSettings", "PORT_ENV",
    "ROLE_ORCHESTRATOR", "SERVICE_NAME", "SESSIONS_PATH", "STATUS_PATH", "Settings", "bearer", "status_body",
]
