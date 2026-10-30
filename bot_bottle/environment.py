"""Named environments on one host (PRD 0116): `BOT_BOTTLE_ENV=<name>`, default
`live`, picks the state root and the prefix of every container, VM, network
and volume, so `dev` and `ci-<run>` never touch the live install.

| Derived | `live` | `<name>` |
|---|---|---|
| state root | `~/.bot-bottle` | `~/.bot-bottle-envs/<name>` |
| resource names | `bot-bottle-*` | `bb-<name>-*` |
| docker gateway subnet | `10.242.255.0/24` | `10.241.<h>.0/24`, `h` from the name |
| docker agent /29s | `10.242.0.0/20` | `10.240.0.0/16`, shared by named envs |
| firecracker pool | the defaults | the slice of a claimed slot (`netpool`) |
| launchd labels, pf anchor | `io.bot-bottle*` | `io.bot-bottle.<name>*` |
| macOS agent IPv6 /64 | `fd62:6f74:6274:6c65::` | `fd62:6f74:6275:<h>::` |
| host controller units | `bot-bottle-hostctl.service` | `bb-<name>-hostctl.service` |

Explicit settings (`BOT_BOTTLE_ROOT`, `BOT_BOTTLE_DOCKER_*`, `BOT_BOTTLE_FC_*`)
still win. Image and artifact caches are content-addressed and shared.

`live` derives exactly today's values. A named environment registers itself
under `~/.bot-bottle-envs/<name>/env.json` the first time a command runs in it;
`bot-bottle envs` lists those and `envs rm` removes one.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "BOT_BOTTLE_ENV"
LIVE = "live"
# Long enough for `ci-<11-digit run id>`; short enough for every name it
# prefixes (systemd units, launchd labels, container names).
MAX_NAME_LENGTH = 24
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

LIVE_ROOT_DIRNAME = ".bot-bottle"
ENVS_DIRNAME = ".bot-bottle-envs"
ENV_FILENAME = "env.json"
_LOCK_FILENAME = ".lock"

LIVE_PREFIX = "bot-bottle-"
NAMED_PREFIX = "bb-"
LIVE_REVERSE_DNS = "io.bot-bottle"
LIVE_MACOS_AGENT_SUBNET_V6 = "fd62:6f74:6274:6c65::/64"

# Docker: live keeps its defaults; named environments use the two /16s below
# it. Each gets its own gateway /24 (picked by name hash), and they share one
# agent pool, since per-agent allocation already skips any subnet in use on
# the daemon.
LIVE_DOCKER_GATEWAY_SUBNET = "10.242.255.0/24"
LIVE_DOCKER_AGENT_SUBNETS = "10.242.0.0/20"
NAMED_DOCKER_GATEWAY_BLOCK = "10.241.0.0/16"
NAMED_DOCKER_AGENT_SUBNETS = "10.240.0.0/16"


class InvalidEnvironmentError(ValueError):
    """A bad environment name, or one that can't be registered."""


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


def macos_agent_subnet_v6(name: str | None = None) -> str:
    """The macOS agent network's IPv6 /64: live's fixed one, else one per
    name (third group `6275`, so never live's)."""
    name = active_name() if name is None else name
    if name == LIVE:
        return LIVE_MACOS_AGENT_SUBNET_V6
    digest = hashlib.sha256(name.encode()).digest()
    return f"fd62:6f74:6275:{int.from_bytes(digest[:2], 'big'):x}::/64"


def envs_dir(home: Path | None = None) -> Path:
    """Where named environments register and keep their state."""
    return (home if home is not None else Path.home()) / ENVS_DIRNAME


def default_root(name: str | None = None, home: Path | None = None) -> Path:
    """The state root `name` uses when `BOT_BOTTLE_ROOT` isn't set."""
    name = active_name() if name is None else name
    base = home if home is not None else Path.home()
    if name == LIVE:
        return base / LIVE_ROOT_DIRNAME
    return envs_dir(base) / name


def _hash_byte(name: str) -> int:
    return hashlib.sha256(name.encode()).digest()[0]


def docker_gateway_subnet(name: str | None = None) -> str:
    """The docker gateway network's default subnet for `name`."""
    name = active_name() if name is None else name
    if name == LIVE:
        return LIVE_DOCKER_GATEWAY_SUBNET
    block = ipaddress.IPv4Network(NAMED_DOCKER_GATEWAY_BLOCK)
    return str(ipaddress.IPv4Network((int(block.network_address) + (_hash_byte(name) << 8), 24)))


def docker_agent_subnets(name: str | None = None) -> str:
    """The pool docker per-agent /29s are carved from for `name`."""
    name = active_name() if name is None else name
    return LIVE_DOCKER_AGENT_SUBNETS if name == LIVE else NAMED_DOCKER_AGENT_SUBNETS


# --- the per-user registry of named environments ---

@dataclass(frozen=True)
class Registration:
    """A named environment as `env.json` records it."""

    name: str
    root: str
    # Firecracker pool slot (1..N), 0 until one is claimed.
    slot: int = 0

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "root": self.root, "slot": self.slot}


def _env_file(name: str, home: Path | None = None) -> Path:
    return envs_dir(home) / name / ENV_FILENAME


def read(name: str, home: Path | None = None) -> Registration | None:
    """`name`'s registration, or None if it has none (or it's unreadable)."""
    try:
        raw = json.loads(_env_file(name, home).read_text(encoding="utf-8"))
        return Registration(name=str(raw["name"]), root=str(raw["root"]),
                            slot=int(raw.get("slot", 0)))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def registered(home: Path | None = None) -> list[Registration]:
    """Every registered named environment, by name."""
    base = envs_dir(home)
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    out: list[Registration] = []
    for n in names:
        reg = read(n, home)
        if reg is not None and reg.name == n:
            out.append(reg)
    return out


def _write(reg: Registration, home: Path | None = None) -> None:
    path = _env_file(reg.name, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{ENV_FILENAME}.{os.getpid()}")
    tmp.write_text(json.dumps(reg.to_json(), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked(home: Path | None = None) -> Generator[None]:
    base = envs_dir(home)
    base.mkdir(parents=True, exist_ok=True)
    with open(base / _LOCK_FILENAME, "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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


def register(name: str, root: Path, home: Path | None = None) -> Registration:
    """Record `name` (idempotent), keeping a claimed slot. Raises
    InvalidEnvironmentError for live or a name whose prefix clashes with a
    registered environment's."""
    validate(name)
    if name == LIVE:
        raise InvalidEnvironmentError("live isn't a named environment")
    with _locked(home):
        existing = read(name, home)
        if existing is not None and existing.root == str(root):
            return existing
        clash = prefix_clash(name, [r.name for r in registered(home)])
        if clash:
            raise InvalidEnvironmentError(
                f"environment {name!r} would share resource names with {clash!r} "
                f"({resource_prefix(name)}* vs {resource_prefix(clash)}*); pick another "
                f"name or remove it with `bot-bottle envs rm {clash}`")
        reg = Registration(name=name, root=str(root),
                           slot=existing.slot if existing is not None else 0)
        _write(reg, home)
        return reg


def ensure_active_registered() -> str:
    """Register the active environment if it's a named one; its name."""
    # pylint: disable-next=import-outside-toplevel
    from .paths import bot_bottle_root
    name = active_name()
    if name != LIVE:
        register(name, bot_bottle_root())
    return name


class NoFreeSlotError(InvalidEnvironmentError):
    """Every reserved firecracker slot is held by another environment."""


def claim_slot(name: str, count: int, home: Path | None = None) -> int:
    """`name`'s firecracker pool slot in 1..`count`: the one it holds, else the
    lowest free one, recorded in its registration. Live is slot 0 and claims
    nothing. Raises NoFreeSlotError when all `count` are held."""
    validate(name)
    if name == LIVE:
        return 0
    with _locked(home):
        mine = read(name, home)
        if mine is not None and 1 <= mine.slot <= count:
            return mine.slot
        held = {r.slot: r.name for r in registered(home) if r.name != name and r.slot}
        for slot in range(1, count + 1):
            if slot not in held:
                root = mine.root if mine is not None else str(default_root(name, home))
                _write(Registration(name=name, root=root, slot=slot), home)
                return slot
    owners = ", ".join(f"{s}: {n}" for s, n in sorted(held.items()))
    raise NoFreeSlotError(
        f"environment {name!r} needs a firecracker network-pool slot, but all {count} "
        f"reserved slots are held ({owners}). Remove one with `bot-bottle envs rm "
        f"<name>`, or reserve more: set BOT_BOTTLE_FC_ENV_SLOTS (or the NixOS "
        f"module's `envSlots`) higher and re-run the network pool setup "
        f"(`bot-bottle install --backend=firecracker`)")


def unregister(name: str, home: Path | None = None) -> None:
    """Forget `name`'s registration (and so free its slot)."""
    validate(name)
    with _locked(home):
        _env_file(name, home).unlink(missing_ok=True)


__all__ = [
    "ENV_VAR", "ENVS_DIRNAME", "LIVE", "LIVE_PREFIX", "MAX_NAME_LENGTH",
    "InvalidEnvironmentError", "NoFreeSlotError", "Registration", "active_name",
    "claim_slot", "default_root", "docker_agent_subnets", "docker_gateway_subnet",
    "ensure_active_registered", "envs_dir", "is_live", "prefix_clash", "read",
    "register", "registered", "resource_prefix", "unregister", "validate",
]
