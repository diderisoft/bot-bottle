"""The optional user service keeping the host controller up across reboots: a
launchd LaunchAgent on macOS, a `systemd --user` unit on Linux. Without it the
CLI starts the daemon on demand. The unit runs the installing package with that
CLI's PATH and root. A clean exit isn't restarted; a crash is.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ...environment import ENV_VAR, LIVE, active_name, resource_prefix, reverse_dns
from ...paths import bot_bottle_root
from .build import PACKAGE_ROOT
from .runner import REFUSED_EXIT_STATUS, HostControlError, state_dir

# Per environment: `io.bot-bottle.hostctl` and `bot-bottle-hostctl.service` for
# live, `io.bot-bottle.<name>.hostctl` and `bb-<name>-hostctl.service` otherwise.
LAUNCHD_LABEL = f"{reverse_dns()}.hostctl"
SYSTEMD_UNIT = f"{resource_prefix()}hostctl.service"


@dataclass(frozen=True)
class ServiceSpec:
    """What the unit runs: the interpreter, and the PATH it should see."""

    python: str
    path_env: str

    def argv(self) -> list[str]:
        return [self.python, "-P", "-m", "bot_bottle.control.hostctl"]  # -P: #706

    def environment(self) -> dict[str, str]:
        env = {"PATH": self.path_env, "PYTHONPATH": str(PACKAGE_ROOT.parent),
               "BOT_BOTTLE_ROOT": str(bot_bottle_root())}
        name = active_name()
        if name != LIVE:
            env[ENV_VAR] = name
        return env


def manager() -> str:
    """`launchd` on macOS, `systemd` on Linux; otherwise unsupported."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise HostControlError(f"no supported service manager on {sys.platform}")


def unit_path(kind: str | None = None) -> Path:
    kind = kind or manager()
    if kind == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def render_launchd(spec: ServiceSpec) -> bytes:
    log = str(state_dir() / "hostctl.log")
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": spec.argv(),
        "EnvironmentVariables": spec.environment(),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        # Agents (tmux, VMs) outlive a controller restart; the next one adopts
        # them.
        "AbandonProcessGroup": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(spec: ServiceSpec) -> str:
    log = state_dir() / "hostctl.log"
    lines = [
        "[Unit]",
        "Description=bot-bottle host controller",
        "",
        "[Service]",
        "ExecStart=" + " ".join(_systemd_quote(a) for a in spec.argv()),
        *(f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in spec.environment().items()),
        "Restart=on-failure",
        "# A corrupt host DB: restarting won't help (`bot-bottle host restore-db`).",
        f"RestartPreventExitStatus={REFUSED_EXIT_STATUS}",
        "# Agents (tmux, VMs) outlive a controller restart; the next one adopts them.",
        "KillMode=process",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def installed() -> bool:
    """True if a unit is installed for this root."""
    try:
        path = unit_path()
        if not path.is_file():
            return False
        root = str(bot_bottle_root())
        if manager() == "launchd":
            env = plistlib.loads(path.read_bytes()).get("EnvironmentVariables", {})
            return env.get("BOT_BOTTLE_ROOT") == root
        return _systemd_quote(f"BOT_BOTTLE_ROOT={root}") in path.read_text(encoding="utf-8")
    except (HostControlError, OSError, ValueError, plistlib.InvalidFileException):
        return False


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise HostControlError(f"{' '.join(argv)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _launchd_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


def install(spec: ServiceSpec) -> Path:
    """Write the unit and load it, which starts the daemon."""
    kind = manager()
    path = unit_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)
    if kind == "launchd":
        _run(["launchctl", "bootout", _launchd_target()], check=False)
        path.write_bytes(render_launchd(spec))
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
    else:
        path.write_text(render_systemd(spec), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
    return path


def uninstall() -> bool:
    """Stop and remove the unit. False if none was installed."""
    kind = manager()
    path = unit_path(kind)
    if not path.is_file():
        return False
    if kind == "launchd":
        _run(["launchctl", "bootout", _launchd_target()], check=False)
    else:
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
    path.unlink()
    if kind == "systemd":
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    return True


def start() -> None:
    """Have the service manager start the daemon."""
    if manager() == "launchd":
        _run(["launchctl", "kickstart", _launchd_target()])
    else:
        _run(["systemctl", "--user", "start", SYSTEMD_UNIT])


__all__ = [
    "LAUNCHD_LABEL", "SYSTEMD_UNIT", "ServiceSpec", "install", "installed", "manager",
    "render_launchd", "render_systemd", "start", "uninstall", "unit_path",
]
