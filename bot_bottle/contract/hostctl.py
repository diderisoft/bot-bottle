"""The host controller's wire contract: routes, responses, its trust domain and
the `Settings` each request carries."""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .auth import ROLE_CLI, ROLE_GATEWAY, TrustDomain

SERVICE_NAME = "bot-bottle-hostctl"

# Daemon port; unset means ephemeral, published in its state file.
PORT_ENV = "BOT_BOTTLE_HOSTCTL_PORT"
# Its port on each backend's host address for the orchestrator. Fixed, so an
# orchestrator's URL survives a daemon restart.
GUEST_PORT = 8098
# That URL, in the orchestrator's env; its token is in
# `HOST_CONTROL.token_env`. Unset: no host controller to relay launches to.
HOSTCTL_URL_ENV = "BOT_BOTTLE_HOSTCTL_URL"

STATUS_PATH = "/status"
# GET /jobs, /jobs/{id}, /jobs/{id}/events (streamed NDJSON).
JOBS_PATH = "/jobs"
# POST /orchestrator/{ensure,restart} start jobs; POST /orchestrator/status
# answers. Each takes {"settings": Settings.to_json()}.
ORCHESTRATOR_PATH = "/orchestrator"
# GET /sessions/{id}: {"running": bool, "exit": {"status", "signal"} | null}.
# GET /sessions/{id}/attach upgrades to the attach WebSocket.
SESSIONS_PATH = "/sessions"
# POST /bottles (a `LaunchRequest`) starts a `bottle.launch` job ({"bottle",
# "session"}); GET lists held bottles ({"id", "backend", "agent", "label",
# "activation", "origin", "writable", "held", "session"}); DELETE
# /bottles/{id} starts a `bottle.teardown` job.
BOTTLES_PATH = "/bottles"
# A session's `origin`: launched by the console over the remote link, or
# anywhere else (`bot-bottle`, `start`, the API).
ORIGIN_CONSOLE = "console"
ORIGIN_TERMINAL = "terminal"
# GET /bottles/live: {"bottles": [{"backend", "slug", "source_ip"}], "errors":
# {backend: message}}. `?backend=` narrows it; a guest listener answers only
# for its own backend. A backend in `errors` couldn't be listed.
LIVE_BOTTLES_PATH = "/bottles/live"
# POST one audit event, `{"producer": ..., "event": {...}}`. Only the
# orchestrator calls it, relaying the gateway's events too.
AUDIT_APPEND_PATH = "/audit/append"
AUDIT_PRODUCERS = frozenset({"orchestrator", "egress", "git-gate", "supervise"})

# A job `error` with this `code` needs a yes/no: resend with `assume_yes`.
CONFIRM_ERROR = "confirm"

# `Authorization: Bearer <token>`.
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"

ROLE_ORCHESTRATOR = "orchestrator"

# The host controller's key. The orchestrator gets an `orchestrator` token
# instead, so it can't mint `cli` tokens.
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
# Where per-agent docker networks are carved from: 512 /29s, below the shared
# gateway network's default `10.242.255.0/24`.
DEFAULT_DOCKER_AGENT_SUBNETS = "10.242.0.0/20"
# Each docker agent's network: docker's router, the gateway and the agent.
DOCKER_AGENT_SUBNET_PREFIX = 29


def agent_subnet_pool(raw: str) -> str:
    """`raw` as a normalized per-agent subnet pool: an IPv4 network with no
    host bits set, /29 or larger. "" is the default pool. Raises ValueError
    saying what's wrong."""
    if not raw.strip():
        return DEFAULT_DOCKER_AGENT_SUBNETS
    try:
        net = ipaddress.ip_network(raw.strip())
    except ValueError as e:
        raise ValueError(f"{raw!r} is not a network in CIDR form, like "
                         f"{DEFAULT_DOCKER_AGENT_SUBNETS} ({e})") from e
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError(f"{raw!r} is IPv6; agent networks are IPv4")
    if net.prefixlen > DOCKER_AGENT_SUBNET_PREFIX:
        raise ValueError(f"{raw!r} is smaller than one /{DOCKER_AGENT_SUBNET_PREFIX}, "
                         "the network each agent needs")
    return str(net)

_SectionT = TypeVar("_SectionT", "OperationSettings", "InfraSettings")


@dataclass(frozen=True)
class OperationSettings:
    """Per-request knobs; `cli` and `orchestrator` may set them."""

    # Empty means auto-select.
    backend: str = ""
    no_cache: bool = False
    cleanup_command_timeout_seconds: float = DEFAULT_CLEANUP_COMMAND_TIMEOUT_SECONDS
    # Firecracker: "" for no per-project workspace disk, else `1` or a size.
    fc_workspace_disk: str = ""


@dataclass(frozen=True)
class InfraSettings:
    """Host infra knobs; only `cli` may set them. "" or 0 means the default."""

    # Build the infra images locally instead of pulling.
    infra_build_local: bool = False
    # `docker`, `podman`, `vm`, or "" for the first usable engine.
    build_engine: str = ""
    artifact_cache: str = ""
    orchestrator_image: str = DEFAULT_ORCHESTRATOR_IMAGE
    gateway_image: str = DEFAULT_GATEWAY_IMAGE
    egress_port: int = DEFAULT_EGRESS_PORT
    docker_gateway_subnet: str = ""
    # Where per-agent docker networks are carved from, as /29s; "" is
    # `10.242.0.0/20`.
    docker_agent_subnets: str = ""
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
    # Named-environment slices the firecracker pool reserves; 0 is the default.
    fc_env_slots: int = 0
    # The host-wide slice claims directory; "" is the default.
    fc_slices_dir: str = ""

    def fingerprint(self) -> str:
        """sha256 of the canonical JSON."""
        canonical = json.dumps(_section_json(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    """Everything bring-up and launch would otherwise read from the env."""

    operation: OperationSettings = field(default_factory=OperationSettings)
    infra: InfraSettings = field(default_factory=InfraSettings)

    def to_json(self) -> dict[str, dict[str, object]]:
        return {"operation": _section_json(self.operation), "infra": _section_json(self.infra)}

    @classmethod
    def from_json(cls, data: object) -> "Settings":
        """Parse a `to_json` value. Missing keys take defaults; unknown keys
        and wrong types raise ValueError."""
        body = _mapping(data, "settings")
        _reject_unknown(body, {"operation", "infra"}, "settings")
        return cls(
            operation=_section_from_json(OperationSettings, body.get("operation", {}), "operation"),
            infra=_section_from_json(InfraSettings, body.get("infra", {}), "infra"),
        )


# Fields left out of the JSON (and so the fingerprint) while at their default:
# ones added after the fingerprint was in use, so an upgrade that doesn't set
# them neither restarts the infra nor sends a key an older reader rejects.
_OMIT_AT_DEFAULT = frozenset({"docker_agent_subnets", "fc_env_slots", "fc_slices_dir",
                              "fc_workspace_disk"})


def _section_json(section: OperationSettings | InfraSettings) -> dict[str, object]:
    body = dataclasses.asdict(section)
    for f in dataclasses.fields(section):
        if f.name in _OMIT_AT_DEFAULT and body[f.name] == f.default:
            del body[f.name]
    return body


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
        # Defaults are plain values of their type; bool isn't int here.
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
# "prefer-cached" (interactive default) uses local images when present, else
# builds or pulls. "cached" fails when one is missing.
IMAGE_POLICIES = ("fresh", "cached", "prefer-cached")
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
    # Headless initial task; empty for interactive.
    prompt: str = ""
    headless: bool = False
    # Identity to resume; empty mints one.
    identity: str = ""
    image_policy: str = "fresh"
    # Skip the yes/no a `CONFIRM_ERROR` would ask for (only a stale image asks).
    assume_yes: bool = False
    settings: Settings = field(default_factory=Settings)
    cwd: str = ""
    copy_cwd: bool = False
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    # Repo name -> the upstream host key the user confirmed.
    host_keys: dict[str, str] = field(default_factory=dict)
    # The console's id for a session it launched; empty for a terminal launch.
    agent_id: str = ""
    # A resume of a bottle with `preserve_container`: a new guest from the
    # agent's image as it is now (its home kept), not the kept one.
    fresh_guest: bool = False

    def to_json(self) -> dict[str, object]:
        body = dataclasses.asdict(self)
        body["bottles"] = list(self.bottles)
        body["settings"] = self.settings.to_json()
        return body

    @classmethod
    def from_json(cls, data: object, role: str) -> "LaunchRequest":
        """Parse and validate a request from `role`. ValueError when malformed;
        PermissionError when a non-`cli` role sets a `cli`-only field."""
        body = _mapping(data, "launch")
        names = {f.name for f in dataclasses.fields(cls)}
        _reject_unknown(body, names, "launch")
        forbidden = sorted(set(body) & set(LAUNCH_CLI_ONLY))
        if role != ROLE_CLI and forbidden:
            raise PermissionError(f"launch: {', '.join(forbidden)} only for the cli role")
        values: dict[str, Any] = {}
        for name in sorted((names - {"bottles", "settings", *_MAP_FIELDS}) & set(body)):
            expected = type(getattr(_DEFAULT_LAUNCH, name))
            # Exact types: bool isn't str, int isn't bool.
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
        """ValueError unless every field is allowed."""
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
    """Shell-style exit code for `status`, or 128 + the number of
    `signal_name`."""
    if signal_name:
        try:
            return 128 + int(signal.Signals[signal_name])
        except KeyError:
            return 128
    return status


def status_body(*, build_id: str, pid: int) -> dict[str, object]:
    """`GET /status`: the daemon's build id and pid."""
    return {"service": SERVICE_NAME, "build_id": build_id, "pid": pid}


def bearer(token: str) -> dict[str, str]:
    """The header carrying `token`."""
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"}


__all__ = [
    "AUDIT_APPEND_PATH", "AUDIT_PRODUCERS",
    "AUTH_HEADER", "AUTH_SCHEME", "BOTTLES_PATH", "BOTTLE_COLORS", "CONFIRM_ERROR",
    "DEFAULT_CLEANUP_COMMAND_TIMEOUT_SECONDS", "DEFAULT_DOCKER_AGENT_SUBNETS",
    "DOCKER_AGENT_SUBNET_PREFIX", "IMAGE_POLICIES", "LAUNCH_CLI_ONLY",
    "LaunchRequest", "agent_subnet_pool", "exit_code",
    "DEFAULT_EGRESS_PORT", "DEFAULT_GATEWAY_IMAGE", "DEFAULT_ORCHESTRATOR_IMAGE", "GUEST_PORT",
    "HOSTCTL_URL_ENV",
    "HOST_CONTROL", "InfraSettings", "JOBS_PATH", "LIVE_BOTTLES_PATH", "ORCHESTRATOR_PATH", "OperationSettings", "PORT_ENV",
    "ORIGIN_CONSOLE", "ORIGIN_TERMINAL",
    "ROLE_ORCHESTRATOR", "SERVICE_NAME", "SESSIONS_PATH", "STATUS_PATH", "Settings", "bearer", "status_body",
]
