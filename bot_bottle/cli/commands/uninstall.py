"""uninstall: remove bot-bottle from this host.

  bot-bottle uninstall [--purge] [--keep-backend] [--force] [--yes]

Shows what it will remove, asks, then:

  1. stops the host controller and every backend's orchestrator and gateway,
     and removes autostart;
  2. removes each backend's containers, networks, volumes and images (frozen
     bottles' images only with --purge);
  3. undoes each backend's privileged setup (the macOS pf firewall, the
     firecracker network pool) unless --keep-backend; where that needs root
     it prints the commands;
  4. removes ~/.cache/bot-bottle (re-downloadable);
  5. removes the package: the installer's venv and shim directly, or with
     pipx/uv if that's how it was installed.

~/.bot-bottle (your agents and bottles, secrets, the audit journal, frozen
bottles, pairing keys) is kept unless --purge. --force stops running
bottles; without it, uninstall refuses while any run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...backend import BottleBackend, get_bottle_backend, has_backend, known_backend_names
from ...backend.host_resources import HostResource
from ...control.hostctl import runner, service_unit
from ...environment import ENV_VAR, active_name, is_live
from ...paths import bot_bottle_root
from ...util import read_tty_line
from ..constants import PROG
from ..settings import settings_from_env

# What `--purge` deletes, for the confirmation.
_DATA = "your agents and bottles, secrets, the audit journal, frozen bottles and pairing keys"


@dataclass(frozen=True)
class _Package:
    """How bot-bottle was installed, and how to remove it."""

    how: str
    remove: list[str] | None = None  # a command to run, or None to delete paths
    paths: tuple[Path, ...] = ()


def _cache_dir() -> Path:
    return Path.home() / ".cache" / "bot-bottle"


def _shims(venv: Path) -> list[Path]:
    """`bot-bottle` links on PATH (or in ~/.local/bin) that point into `venv`
    (a venv, or the installer's private uv)."""
    found = []
    dirs = os.environ.get("PATH", "").split(os.pathsep) + [str(Path.home() / ".local" / "bin")]
    for directory in dict.fromkeys(dirs):
        shim = Path(directory or ".") / "bot-bottle"
        try:
            if shim.is_symlink() and shim.resolve().is_relative_to(venv.resolve()):
                found.append(shim)
        except OSError:
            continue
    return found


def _package() -> _Package:
    """The install method, from where this interpreter lives."""
    prefix = Path(sys.prefix).resolve()
    venv = Path(os.environ.get("BOT_BOTTLE_VENV") or bot_bottle_root() / "venv").resolve()
    if prefix == venv:
        return _Package(f"the installer's venv at {venv}", paths=(*_shims(venv), venv))
    # The installer's own uv, with the Python and tool it installed (#694).
    private_uv = (bot_bottle_root() / "uv").resolve()
    if prefix.is_relative_to(private_uv):
        return _Package(f"the installer's uv at {private_uv}",
                        paths=(*_shims(private_uv), private_uv))
    parts = prefix.parts
    if "pipx" in parts and "venvs" in parts:
        return _Package("pipx", remove=["pipx", "uninstall", "bot-bottle"])
    if "uv" in parts and "tools" in parts:
        return _Package("uv", remove=["uv", "tool", "uninstall", "bot-bottle"])
    return _Package(f"{prefix} (a checkout or pip install, which uninstall leaves alone)")


def _backends() -> list[BottleBackend[Any, Any]]:
    settings = settings_from_env()
    return [get_bottle_backend(settings, name) for name in known_backend_names()
            if has_backend(settings, name)]


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")


def _plan(resources: dict[str, list[HostResource]], package: _Package, ns: argparse.Namespace,
          ) -> None:
    _say("This removes bot-bottle from this host:")
    _say("  - the host controller, each backend's orchestrator and gateway, and autostart")
    for name, found in resources.items():
        if found:
            kinds: dict[str, int] = {}
            for r in found:
                kinds[r.what.split()[0]] = kinds.get(r.what.split()[0], 0) + 1
            listed = ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())
            _say(f"  - {name}: {listed}")
    if not ns.keep_backend:
        _say("  - each backend's privileged setup (macOS: the pf firewall; firecracker: "
             "the network pool), with sudo where needed")
    if _cache_dir().exists():
        _say(f"  - {_cache_dir()} (downloaded artifacts and caches)")
    _say(f"  - the package, installed with {package.how}")
    root = bot_bottle_root()
    if ns.purge:
        _say(f"  - {root}: {_DATA}")
    elif root.exists():
        _say(f"It keeps {root} ({_DATA}); --purge removes that too.")


def _confirm(ns: argparse.Namespace) -> bool:
    if ns.yes:
        return True
    question = "Remove all of this, including your data?" if ns.purge else "Remove all of this?"
    sys.stdout.write(f"\n{question} [y/N] ")
    sys.stdout.flush()
    return read_tty_line().strip().lower() in ("y", "yes")


def _stop(backends: list[BottleBackend[Any, Any]], force: bool) -> bool:
    """Stop the host controller and infra; False (reported) when bottles run
    and not `force`."""
    state = runner.running()
    held = runner.held_bottles(state) if state else []
    if held and not force:
        sys.stderr.write(f"{PROG}: {len(held)} bottle(s) are running ({', '.join(held)}); "
                         f"tear them down first (`{PROG} cleanup bots`) or use --force\n")
        return False
    for backend in backends:
        try:
            backend.infra_service().stop()
        except Exception as e:  # pylint: disable=broad-exception-caught  # removal goes on
            sys.stderr.write(f"{PROG}: couldn't stop {backend.name}'s infra: {e}\n")
    try:
        runner.stop(force=force)
    except runner.HostControlError as e:
        sys.stderr.write(f"{PROG}: {e}\n")
    _say("stopped: host controller, orchestrators and gateways")
    _say("autostart: removed" if service_unit.uninstall() else "autostart: not installed")
    return True


def _remove_package(package: _Package) -> bool:
    if package.remove is not None:
        if shutil.which(package.remove[0]) is None:
            _say(f"package: remove it with `{' '.join(package.remove)}`")
            return False
        ok = subprocess.run(package.remove, check=False).returncode == 0
        _say("package: removed" if ok else f"package: `{' '.join(package.remove)}` failed")
        return ok
    if not package.paths:
        _say(f"package: left in place ({package.how})")
        return True
    for path in package.paths:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
        _say(f"removed {path}")
    return True


def cmd_uninstall(argv: list[str]) -> int:  # pylint: disable=too-many-branches
    parser = argparse.ArgumentParser(prog=f"{PROG} uninstall", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--purge", action="store_true",
                        help=f"also delete {bot_bottle_root()} and frozen bottles' images")
    parser.add_argument("--keep-backend", action="store_true",
                        help="leave each backend's privileged setup in place")
    parser.add_argument("--force", action="store_true", help="even while bottles are running")
    parser.add_argument("--yes", "-y", action="store_true", help="don't ask")
    ns = parser.parse_args(argv)

    if not is_live():
        # Uninstall removes the shared caches, the package and the host setup
        # every environment uses; one environment goes with `envs rm`.
        sys.stderr.write(f"{PROG} uninstall: {ENV_VAR}={active_name()} is a named environment; "
                         f"remove it with `{PROG} envs rm {active_name()}`, or unset "
                         f"{ENV_VAR} to uninstall bot-bottle\n")
        return 2

    backends = _backends()
    resources = {b.name: b.host_resources(purge=ns.purge) for b in backends}
    package = _package()
    _plan(resources, package, ns)
    if not _confirm(ns):
        _say("uninstall: nothing removed")
        return 1
    if not _stop(backends, ns.force):
        return 1

    failed: list[str] = []
    for found in resources.values():
        for resource in found:
            if not resource.remove():
                failed.append(resource.what)
    removed = sum(len(f) for f in resources.values()) - len(failed)
    _say(f"backend resources: removed {removed}"
         + (f"; couldn't remove {', '.join(failed)}" if failed else ""))

    if not ns.keep_backend:
        for backend in backends:
            backend.teardown()  # prints the sudo steps where it needs root

    if _cache_dir().exists():
        shutil.rmtree(_cache_dir(), ignore_errors=True)
        _say(f"removed {_cache_dir()}")
    ok = _remove_package(package)
    if ns.purge and bot_bottle_root().exists():
        shutil.rmtree(bot_bottle_root(), ignore_errors=True)
        _say(f"removed {bot_bottle_root()}")
    return 0 if ok and not failed else 1


__all__ = ["cmd_uninstall"]
