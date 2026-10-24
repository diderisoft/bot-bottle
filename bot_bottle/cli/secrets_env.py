"""The CLI's `SecretSource`: this process's env, then the host secrets file.
Built once per command."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ..paths import host_secrets_path
from ..secret_source import ChainSecrets, FileSecrets, MappingSecrets


def secrets_from_env(
    environ: Mapping[str, str] | None = None, *, secrets_file: Path | None = None,
) -> ChainSecrets:
    """`environ` (default: this process's), then `secrets_file` (default: the
    host file)."""
    env = os.environ if environ is None else environ
    path = host_secrets_path() if secrets_file is None else secrets_file
    return ChainSecrets(MappingSecrets(dict(env)), FileSecrets(path))


__all__ = ["secrets_from_env"]
