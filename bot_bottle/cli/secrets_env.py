"""The CLI's `SecretSource`: this process's env, then the host secrets store,
kept apart (`LaunchSecrets`) so `${VAR}` reads only the env and `secret:NAME`
only the store. Built once per command."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ..secret_source import LaunchSecrets, MappingSecrets, host_store


def secrets_from_env(
    environ: Mapping[str, str] | None = None, *, secrets_file: Path | None = None,
) -> LaunchSecrets:
    """`environ` (default: this process's), then `secrets_file` (default: the
    host store)."""
    env = os.environ if environ is None else environ
    return LaunchSecrets(MappingSecrets(dict(env)), host_store(secrets_file))


__all__ = ["secrets_from_env"]
