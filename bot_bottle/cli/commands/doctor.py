"""doctor: check host prerequisites. Fails only without a new-enough Python, a
ready backend, or tmux; the config directory is advisory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ...agent_provider import get_provider
from ...backend import (
    backend_os_requirement, docker_isolation_summary, is_backend_ready, known_backend_names,
)
from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
from .. import openrouter
from ...control.hostctl.sessions import MIN_TMUX_VERSION, tmux_version
from ..constants import PROG
from ..settings import settings_from_env

MIN_PYTHON = (3, 11)
CONFIG_DIR = ".bot-bottle"


def _ok(label: str, detail: str) -> None:
    print(f"ok:   {label}: {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"warn: {label}: {detail}")


def _fail(label: str, detail: str) -> None:
    print(f"fail: {label}: {detail}")


def _note(label: str, detail: str) -> None:
    print(f"note: {label}: {detail}")


def _check_python() -> bool:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        _ok("python", detail)
        return True
    _fail("python", f"{detail}; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")
    return False


def _check_backends() -> bool:
    """At least one backend must pass its full `status()` checks, not just have
    a binary on PATH. Each not-ready backend prints what's missing; one that
    can't run on this OS is skipped in a line."""
    settings = settings_from_env()
    ready = []
    for name in known_backend_names():
        needs = backend_os_requirement(name)
        if needs:
            _note("backend", f"{name}: skipped ({needs} only)")
        elif is_backend_ready(settings, name, quiet=False):
            _ok("backend", f"{name}: ready")
            ready.append(name)
        else:
            _warn("backend", f"{name}: not ready (see diagnostics above)")
    if "docker" in ready and (settings.operation.backend == "docker" or ready == ["docker"]):
        _note("backend", docker_isolation_summary())
    if ready:
        return True
    _fail(
        "backend",
        "no backend is ready to run a bottle; start Docker, or finish "
        "Apple Container (macOS) / Firecracker (Linux) setup",
    )
    return False


def _check_tmux() -> bool:
    """Agent sessions run in tmux; 3.1 added `window-size latest`."""
    version = tmux_version()
    if version is None:
        _fail("tmux", "not installed; install tmux 3.1 or newer (brew/apt/nix install tmux)")
        return False
    detail = f"{version[0]}.{version[1]}"
    if version < MIN_TMUX_VERSION:
        _fail("tmux", f"{detail}; need {MIN_TMUX_VERSION[0]}.{MIN_TMUX_VERSION[1]} or newer")
        return False
    _ok("tmux", detail)
    return True


def _check_config_dir() -> None:
    config = Path.home() / CONFIG_DIR
    if config.is_dir():
        _ok("config", str(config))
    else:
        _warn("config", f"{config} does not exist yet (created on first use)")


def _check_providers() -> None:
    """What `setup` would find for each provider and whether its starter
    exists. Advisory; never writes."""
    env = dict(os.environ)
    manifests = Path.home() / CONFIG_DIR
    missing: list[str] = []
    for name in ("claude", "codex", "pi"):
        detection = get_provider(name).detect_host(env)
        written = all((manifests / kind / f"{name}.md").exists() for kind in ("agents", "bottles"))
        if name != "pi" and not detection.installed:
            continue
        if name != "pi" and not detection.logged_in:
            _warn(name, f"installed but not logged in; run `{detection.login_command}`")
        elif written:
            _ok(name, f"starter in {manifests} ({detection.login_location or 'ready'})")
        else:
            missing.append(name)
            _warn(name, "no starter yet" + (f" (login: {detection.login_location})"
                                             if detection.login_location else ""))
        if name == "pi" and written:
            _check_pi_model(manifests / "agents" / "pi.md")
    if missing:
        print(f"      run `{PROG} setup` to write starters for: {', '.join(missing)}")


def _check_pi_model(agent: Path) -> None:
    """Warn when the pi starter's free OpenRouter model has left the free list."""
    try:
        frontmatter, _ = parse_frontmatter(agent.read_text())
    except (OSError, YamlSubsetError):
        return
    provider = frontmatter.get("agent_provider")
    settings = provider.get("settings") if isinstance(provider, dict) else None
    models = settings.get("models") if isinstance(settings, dict) else None
    if not isinstance(settings, dict) or settings.get("provider") != "openrouter" \
            or not isinstance(models, list) or not models:
        return
    model = str(models[0])
    if not model.endswith(":free"):
        return
    try:
        still_free = openrouter.listed(model)
    except openrouter.OpenRouterError:
        return  # offline
    if not still_free:
        _warn("pi", f"{model} is no longer a free OpenRouter model; "
                    f"run `{PROG} setup --refresh-model`")


def cmd_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} doctor",
        description="Check host prerequisites for running bot-bottle.",
    )
    parser.parse_args(argv)

    # Hard requirements set the exit code.
    required = [_check_python(), _check_backends(), _check_tmux()]
    _check_config_dir()
    _check_providers()
    return 0 if all(required) else 1
