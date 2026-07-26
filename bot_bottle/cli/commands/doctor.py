"""`doctor` CLI command — validate host prerequisites for running
bot-bottle and report what's ready.

Fails (non-zero exit) only on the two hard requirements: a new-enough
Python and at least one backend that is *ready* (passes its full status
checks, so `start` can actually work). The config directory is a soft
check — `install.sh` creates it, but a missing one only warrants a note,
not a failure, since `start` provisions what it needs on first run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ...backend import is_backend_ready, known_backend_names
from ..constants import PROG

MIN_PYTHON = (3, 11)
CONFIG_DIR = ".bot-bottle"


def _ok(label: str, detail: str) -> None:
    print(f"ok:   {label}: {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"warn: {label}: {detail}")


def _fail(label: str, detail: str) -> None:
    print(f"fail: {label}: {detail}")


def _check_python() -> bool:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        _ok("python", detail)
        return True
    _fail("python", f"{detail}; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")
    return False


def _check_backends() -> bool:
    """At least one backend must be *ready* to run a bottle — i.e. pass its
    full status() checks (daemon reachable, network pool present, KVM usable),
    not merely have a binary on PATH. A binary-only check would report `ok`
    on a host with a stopped Docker daemon or a half-configured Firecracker,
    where `start` still can't work. Each not-ready backend prints its own
    diagnostics (quiet=False) so the operator sees exactly what's missing."""
    ready = []
    for name in known_backend_names():
        if is_backend_ready(name, quiet=False):
            _ok("backend", f"{name}: ready")
            ready.append(name)
        else:
            _warn("backend", f"{name}: not ready (see diagnostics above)")
    if ready:
        return True
    _fail(
        "backend",
        "no backend is ready to run a bottle; start Docker, or finish "
        "Apple Container (macOS) / Firecracker (Linux) setup",
    )
    return False


def _check_config_dir() -> None:
    config = Path.home() / CONFIG_DIR
    if config.is_dir():
        _ok("config", str(config))
    else:
        _warn("config", f"{config} does not exist yet (created on first use)")


def cmd_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} doctor",
        description="Check host prerequisites for running bot-bottle.",
    )
    parser.parse_args(argv)

    # Hard requirements gate the exit code; the config note is advisory.
    required = [_check_python(), _check_backends()]
    _check_config_dir()
    return 0 if all(required) else 1
