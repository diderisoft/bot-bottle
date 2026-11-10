"""Named environments on one host (PRD 0116): `BOT_BOTTLE_ENV=<name>`, default
`live`, picks the state root and the prefix of every container, VM, network
and volume, so `dev` and `ci-<run>` never touch the live install.

| Derived | `live` | `<name>` |
|---|---|---|
| state root | `~/.bot-bottle` | `~/.bot-bottle-envs/<name>` |
| resource names | `bot-bottle-*` | `bb-<name>-*` |
| docker gateway subnet | `10.242.255.0/24` | a free /24 of `10.241.0.0/16`, recorded |
| docker agent /29s | `10.242.0.0/20` | `10.240.0.0/16`, shared by named envs |
| docker orchestrator host port | `127.0.0.1:8099` | a free port of 18100-18899, recorded |
| firecracker pool | the defaults | a host-wide claimed slice (`slice_claims`) |
| launchd labels, pf anchor | `io.bot-bottle*` | `io.bot-bottle.<name>*` |
| macOS agent IPv6 /64 | `fd62:6f74:6274:6c65::` | a free `fd62:6f74:6275:<n>::`, recorded |
| host controller units | `bot-bottle-hostctl.service` | `bb-<name>-hostctl.service` |

Explicit settings (`BOT_BOTTLE_ROOT`, `BOT_BOTTLE_DOCKER_*`, `BOT_BOTTLE_FC_*`)
still win. Image and artifact caches are content-addressed and shared.

`live` derives exactly today's values. A named environment is its directory,
`~/.bot-bottle-envs/<name>`, created the first time a command runs in it;
`bot-bottle envs` lists those directories and `envs rm` removes one. Nothing
else records it: everything above derives from the name, and what an
environment allocates is recorded in its host DB or, for the macOS /64, the
pf rules.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

ENV_VAR = "BOT_BOTTLE_ENV"
LIVE = "live"
# Long enough for `ci-<11-digit run id>`; short enough for every name it
# prefixes (systemd units, launchd labels, container names).
MAX_NAME_LENGTH = 24
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

LIVE_ROOT_DIRNAME = ".bot-bottle"
ENVS_DIRNAME = ".bot-bottle-envs"

LIVE_PREFIX = "bot-bottle-"
NAMED_PREFIX = "bb-"
LIVE_REVERSE_DNS = "io.bot-bottle"
LIVE_MACOS_AGENT_SUBNET_V6 = "fd62:6f74:6274:6c65::/64"

# Docker: live keeps its defaults; named environments use the two /16s below
# it. Each allocates a gateway /24 (trying the /24s in an order that starts at
# its name's hash, so environments rarely contend) and records it; they share
# one agent pool, since per-agent allocation already skips any subnet in use
# on the daemon.
LIVE_DOCKER_GATEWAY_SUBNET = "10.242.255.0/24"
LIVE_DOCKER_AGENT_SUBNETS = "10.242.0.0/20"
NAMED_DOCKER_GATEWAY_BLOCK = "10.241.0.0/16"
NAMED_DOCKER_AGENT_SUBNETS = "10.240.0.0/16"
# The docker orchestrator's host-side (loopback) port: live's fixed one, and
# the range named environments allocate theirs from.
LIVE_DOCKER_ORCHESTRATOR_PORT = 8099
NAMED_DOCKER_PORT_BASE = 18100
NAMED_DOCKER_PORT_COUNT = 800


class InvalidEnvironmentError(ValueError):
    """A bad environment name, or one that can't be created."""


def validate(name: str) -> str:
    """`name` if it's a usable environment name; raises InvalidEnvironmentError
    saying why not."""
    if not name:
        raise InvalidEnvironmentError(f"{ENV_VAR} is empty")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidEnvironmentError(
            f"{ENV_VAR}={name!r} is longer than {MAX_NAME_LENGTH} characters")
    if not _NAME_RE.match(name):
        raise InvalidEnvironmentError(
            f"{ENV_VAR}={name!r}: use lowercase letters, digits and `-`, "
            "starting and ending with a letter or digit")
    return name


def active_name(environ: Mapping[str, str] | None = None) -> str:
    """The active environment: `$BOT_BOTTLE_ENV`, validated, else `live`."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_VAR, "").strip()
    return validate(raw) if raw else LIVE


def is_live(name: str | None = None) -> bool:
    return (active_name() if name is None else name) == LIVE


def resource_prefix(name: str | None = None) -> str:
    """The prefix of every container, VM, network and volume the environment
    creates: `bot-bottle-` for live, `bb-<name>-` otherwise."""
    name = active_name() if name is None else name
    return LIVE_PREFIX if name == LIVE else f"{NAMED_PREFIX}{name}-"


def reverse_dns(name: str | None = None) -> str:
    """The reverse-DNS stem of launchd labels and the macOS pf anchor:
    `io.bot-bottle` for live, `io.bot-bottle.<name>` otherwise."""
    name = active_name() if name is None else name
    return LIVE_REVERSE_DNS if name == LIVE else f"{LIVE_REVERSE_DNS}.{name}"


def macos_agent_subnet_v6_candidates(name: str | None = None) -> Iterator[str]:
    """The macOS agent network's IPv6 /64s to try, in order: live's fixed one,
    else every `fd62:6f74:6275:<n>::/64` (never live's group) starting at the
    name's hash. Setup records the one it installs in the pf rules."""
    name = active_name() if name is None else name
    if name == LIVE:
        yield LIVE_MACOS_AGENT_SUBNET_V6
        return
    start = int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "big")
    for i in range(1 << 16):
        yield f"fd62:6f74:6275:{(start + i) % (1 << 16):x}::/64"


def macos_agent_subnet_v6(name: str | None = None) -> str:
    """The first candidate: what an environment uses before setup records one."""
    return next(macos_agent_subnet_v6_candidates(name))


def envs_dir(home: Path | None = None) -> Path:
    """Where named environments live, one directory each."""
    return (home if home is not None else Path.home()) / ENVS_DIRNAME


def default_root(name: str | None = None, home: Path | None = None) -> Path:
    """The state root `name` uses when `BOT_BOTTLE_ROOT` isn't set."""
    name = active_name() if name is None else name
    base = home if home is not None else Path.home()
    if name == LIVE:
        return base / LIVE_ROOT_DIRNAME
    return envs_dir(base) / name


def docker_gateway_candidates(name: str | None = None) -> Iterator[str]:
    """The docker gateway subnets to try, in order: live's fixed one, else
    every /24 of `10.241.0.0/16` starting at the name's hash."""
    name = active_name() if name is None else name
    if name == LIVE:
        yield LIVE_DOCKER_GATEWAY_SUBNET
        return
    block = ipaddress.IPv4Network(NAMED_DOCKER_GATEWAY_BLOCK)
    start = hashlib.sha256(name.encode()).digest()[0]
    for i in range(256):
        yield str(ipaddress.IPv4Network(
            (int(block.network_address) + (((start + i) % 256) << 8), 24)))


def docker_gateway_subnet(name: str | None = None) -> str:
    """The first candidate: what an environment tries before one is recorded."""
    return next(docker_gateway_candidates(name))


def docker_orchestrator_port_candidates(name: str | None = None) -> Iterator[int]:
    """The docker orchestrator's host ports to try, in order: live's fixed
    one, else every port of the named range starting at the name's hash."""
    name = active_name() if name is None else name
    if name == LIVE:
        yield LIVE_DOCKER_ORCHESTRATOR_PORT
        return
    start = int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "big")
    for i in range(NAMED_DOCKER_PORT_COUNT):
        yield NAMED_DOCKER_PORT_BASE + (start + i) % NAMED_DOCKER_PORT_COUNT


def docker_agent_subnets(name: str | None = None) -> str:
    """The pool docker per-agent /29s are carved from for `name`."""
    name = active_name() if name is None else name
    return LIVE_DOCKER_AGENT_SUBNETS if name == LIVE else NAMED_DOCKER_AGENT_SUBNETS


# --- named environments: one directory each ---

def env_dir(name: str, home: Path | None = None) -> Path:
    """`name`'s directory, `~/.bot-bottle-envs/<name>`. Its existence is the
    environment: a host-wide firecracker slice claim names it, so its absence
    marks the claim stale."""
    return envs_dir(home) / name


def named(home: Path | None = None) -> list[str]:
    """Every named environment, by name: the directories under
    `~/.bot-bottle-envs/` with a valid name."""
    try:
        entries = list(envs_dir(home).iterdir())
    except OSError:
        return []
    out: list[str] = []
    for p in entries:
        try:
            validate(p.name)
        except InvalidEnvironmentError:
            continue
        if p.name != LIVE and p.is_dir():
            out.append(p.name)
    return sorted(out)


def prefix_clash(name: str, others: list[str]) -> str:
    """Another environment whose resource prefix would match `name`'s
    resources, or the reverse (`dev` and `dev-2`: `bb-dev-` matches
    `bb-dev-2-orchestrator`); "" when there's none."""
    mine = resource_prefix(name)
    for other in others:
        if other == name:
            continue
        theirs = resource_prefix(other)
        if mine.startswith(theirs) or theirs.startswith(mine):
            return other
    return ""


def create(name: str, home: Path | None = None) -> Path:
    """`name`'s directory, created if it's new (idempotent). Raises
    InvalidEnvironmentError for live, or for a new name whose prefix clashes
    with an existing environment's. The check runs after the `mkdir`, so two
    clashing names created at once both see the other and both back out."""
    validate(name)
    if name == LIVE:
        raise InvalidEnvironmentError("live isn't a named environment")
    path = env_dir(name, home)
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        return path
    clash = prefix_clash(name, named(home))
    if clash:
        path.rmdir()
        raise InvalidEnvironmentError(
            f"environment {name!r} would share resource names with {clash!r} "
            f"({resource_prefix(name)}* vs {resource_prefix(clash)}*); pick another "
            f"name or remove it with `bot-bottle envs rm {clash}`")
    return path


def sudo_invoker() -> tuple[int, int] | None:
    """The real user's `(uid, gid)` when this runs as root through sudo, else
    None. Under sudo only the root-owned system pieces are written: per-user
    state left behind as root would lock that user out of it."""
    if os.geteuid() != 0:
        return None
    try:
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        return None
    return None if uid == 0 else (uid, gid)


def root_owned_state(home: Path | None = None) -> list[Path]:
    """Per-user state a run as root left behind (e.g. an earlier
    `sudo bot-bottle install`), which this user can no longer write. Checks
    the active state root and its top-level entries."""
    if os.geteuid() == 0:
        return []
    # pylint: disable-next=import-outside-toplevel
    from .paths import bot_bottle_root
    root = bot_bottle_root(home)
    candidates = [root]
    try:
        candidates += sorted(root.iterdir())
    except OSError:
        pass
    found: list[Path] = []
    for path in candidates:
        try:
            if path.lstat().st_uid == 0:
                found.append(path)
        except OSError:
            continue
    return found


def root_owned_fix(paths: list[Path]) -> str:
    """The command that gives `paths` back to the current user."""
    targets = " ".join(sorted({str(p) for p in paths}))
    return f'sudo chown -R "$(id -un):$(id -gn)" {targets}'


def ensure_active() -> str:
    """Create the active environment's directory if it's a named one; its
    name."""
    name = active_name()
    if name != LIVE:
        create(name)
    return name


__all__ = [
    "ENV_VAR", "ENVS_DIRNAME", "LIVE", "LIVE_PREFIX", "MAX_NAME_LENGTH",
    "InvalidEnvironmentError", "active_name", "create", "default_root",
    "docker_agent_subnets", "docker_gateway_candidates", "docker_gateway_subnet",
    "docker_orchestrator_port_candidates", "ensure_active", "env_dir", "envs_dir",
    "is_live", "macos_agent_subnet_v6", "macos_agent_subnet_v6_candidates", "named",
    "prefix_clash", "resource_prefix", "reverse_dns", "root_owned_fix", "root_owned_state",
    "sudo_invoker", "validate",
]
