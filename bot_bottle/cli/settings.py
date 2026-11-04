"""Host `Settings` from the `BOT_BOTTLE_*` env vars, built once per command:
the one place host code reads behaviour knobs from the env.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping
from typing import Any

from ..contract.hostctl import (
    DEFAULT_DOCKER_AGENT_SUBNETS, InfraSettings, OperationSettings, Settings,
    agent_subnet_pool,
)
from ..lib.log import die

# Settings field -> env var. A field with no env var keeps its default here;
# a flag (`--no-cache`) or the code sets it. Every name is described in
# `bot_bottle/env_vars.py`.
OPERATION_ENV: dict[str, str] = {
    "backend": "BOT_BOTTLE_BACKEND",
    "fc_workspace_disk": "BOT_BOTTLE_FC_WORKSPACE_DISK",
}

INFRA_ENV: dict[str, str] = {
    "infra_build_local": "BOT_BOTTLE_INFRA_BUILD",
    "build_engine": "BOT_BOTTLE_BUILD_ENGINE",
    "artifact_cache": "BOT_BOTTLE_ARTIFACT_CACHE",
    "orchestrator_image": "BOT_BOTTLE_ORCHESTRATOR_IMAGE",
    "gateway_image": "BOT_BOTTLE_GATEWAY_IMAGE",
    "docker_gateway_subnet": "BOT_BOTTLE_DOCKER_GATEWAY_SUBNET",
    "docker_agent_subnets": "BOT_BOTTLE_DOCKER_AGENT_SUBNETS",
    "docker_ca_mount": "BOT_BOTTLE_DOCKER_CA_MOUNT",
    "docker_root_mount": "BOT_BOTTLE_DOCKER_ROOT_MOUNT",
    "docker_client_network": "BOT_BOTTLE_DOCKER_CLIENT_NETWORK",
    "fc_kernel": "BOT_BOTTLE_FC_KERNEL",
    "fc_dropbear": "BOT_BOTTLE_FC_DROPBEAR",
    "fc_pool_size": "BOT_BOTTLE_FC_POOL_SIZE",
    "fc_ip_base": "BOT_BOTTLE_FC_IP_BASE",
    "fc_iface_prefix": "BOT_BOTTLE_FC_IFACE_PREFIX",
    "fc_nft_table": "BOT_BOTTLE_FC_NFT_TABLE",
    "fc_orch_iface": "BOT_BOTTLE_FC_ORCH_IFACE",
    "fc_gw_iface": "BOT_BOTTLE_FC_GW_IFACE",
    "fc_env_slots": "BOT_BOTTLE_FC_ENV_SLOTS",
    "fc_slices_dir": "BOT_BOTTLE_FC_SLICES_DIR",
    "macos_container_dns": "BOT_BOTTLE_MACOS_CONTAINER_DNS",
}


def settings_from_env(environ: Mapping[str, str] | None = None) -> Settings:
    """`Settings` from `environ` (default: this process's). Unset or empty vars
    keep the default."""
    env = os.environ if environ is None else environ
    return Settings(
        operation=OperationSettings(**_section(OperationSettings, OPERATION_ENV, env)),
        infra=InfraSettings(**_section(InfraSettings, INFRA_ENV, env)),
    )


def _section(
    cls: type[OperationSettings] | type[InfraSettings],
    names: Mapping[str, str],
    env: Mapping[str, str],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        var = names.get(f.name)
        if var is None:
            continue
        raw = env.get(var, "").strip()
        if raw:
            values[f.name] = _parse(f.name, var, raw, f.default)
    return values


def _parse(name: str, var: str, raw: str, default: object) -> object:
    if name in _SPECIAL:
        return _SPECIAL[name](raw, default)
    if isinstance(default, bool):
        return raw == "1"
    if isinstance(default, int):
        return _non_negative_int(var, raw)
    return raw


def _non_negative_int(var: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        die(f"{var} must be an integer")
    if value < 0:
        die(f"{var} must not be negative")
    return value


def _agent_subnets(raw: str, _default: object) -> object:
    try:
        pool = agent_subnet_pool(raw)
    except ValueError as e:
        die(f"BOT_BOTTLE_DOCKER_AGENT_SUBNETS: {e}")
    # Spelling out the default is the default: it leaves the fingerprint be.
    return "" if pool == DEFAULT_DOCKER_AGENT_SUBNETS else pool


# Fields whose env values need custom parsing.
_SPECIAL: dict[str, Callable[[str, object], object]] = {
    "infra_build_local": lambda raw, _default: raw.lower() == "local",
    "build_engine": lambda raw, _default: raw.lower(),
    "docker_agent_subnets": _agent_subnets,
}


__all__ = ["INFRA_ENV", "OPERATION_ENV", "settings_from_env"]
