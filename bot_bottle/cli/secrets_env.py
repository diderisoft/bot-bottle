"""Build the CLI's `SecretSource`: this process's environment,
then the host secrets file. The CLI calls `secrets_from_env()` once per
command and passes the result down, like `settings_from_env()`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ..paths import host_secrets_path
from ..secret_source import ChainSecrets, FileSecrets, MappingSecrets


def secrets_from_env(
    environ: Mapping[str, str] | None = None, *, secrets_file: Path | None = None,
) -> ChainSecrets:
    """`environ` (default: this process's) first, then `secrets_file`
    (default: the host secrets file)."""
    env = os.environ if environ is None else environ
    path = host_secrets_path() if secrets_file is None else secrets_file
    return ChainSecrets(MappingSecrets(dict(env)), FileSecrets(path))


__all__ = ["secrets_from_env"]
