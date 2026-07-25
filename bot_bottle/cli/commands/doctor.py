"""`doctor` CLI command — validate host prerequisites for running
bot-bottle and report what's ready.

Fails (non-zero exit) only on the two hard requirements: a new-enough
Python and at least one usable backend. The config directory is a soft
check — `install.sh` creates it, but a missing one only warrants a note,
not a failure, since `start` provisions what it needs on first run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ...backend import is_backend_available, known_backend_names
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
    """At least one backend must be available on this host. Report each
    known backend so the operator sees why a missing one is missing."""
    available = []
    for name in known_backend_names():
        if is_backend_available(name):
            available.append(name)
    if available:
        _ok("backend", f"available: {', '.join(available)}")
        return True
    _fail(
        "backend",
        "no backend available; install Apple Container (macOS), "
        "Firecracker (Linux), or Docker",
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
