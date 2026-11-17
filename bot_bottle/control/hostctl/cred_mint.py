"""Minting and re-adopting `credentials:` block entries.

`mint_cred_block` is called between `backend.prepare()` and `backend.launch()`
in the host controller launch path.  `recredential` and `recredential_block`
are called from `BottleService.adopt()` to re-establish the credential state
for bottles that survived a daemon restart.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...credentials import (
    KIND_TOKEN, CredentialProvider, CredentialRegistry, MintedCredential, default_registry,
)
from ...egress.service import cred_token_ref
from ...lib import log
from ...paths import host_secrets_path
from ...secret_source import SecretSource

if TYPE_CHECKING:
    from ...backend.plan import BottlePlan
    from ...manifest.credentials import ManifestCredential

# (label, minted, provider, settings, renewal_secrets, slots)
# `slots` maps each kind field (usually "token") to the EGRESS_TOKEN_N names
# that carry it; the CredentialRenewer uses these when pushing renewed values.
CredEntry = tuple[
    str,
    MintedCredential,
    CredentialProvider,
    Mapping[str, object],
    SecretSource,
    dict[str, tuple[str, ...]],
]


def mint_cred_block(
    plan: "BottlePlan",
    identity: str,
    launch_secrets: SecretSource,
    *,
    registry: CredentialRegistry | None = None,
) -> tuple["BottlePlan", list[CredEntry]]:
    """Mint all `credentials:` block entries that need live values.

    - `source: secret` credentials are skipped: they already resolve from the
      host secrets store via `EgressTokenSecrets._route_refs`.
    - All other sources (host_login, prompt, …) are minted here; their token
      values are injected into `plan.minted_cred_values` so `egress_secrets`
      can find them when `launch()` calls `registration_tokens()`.
    - The returned `cred_entries` list is passed to `CredentialRenewer.add()`
      after the bottle is running.
    - `launch_secrets` (the launch-time `LaunchSecrets` with `request.answers`)
      is used for `mint()`; renewal uses a fresh `FileSecrets` so the launch's
      transient answers aren't captured.
    """
    reg = registry or default_registry()
    credentials = getattr(plan.manifest.bottle, "credentials", {})
    if not credentials:
        return plan, []

    from ...secret_source import FileSecrets  # pylint: disable=import-outside-toplevel
    renewal_secrets = FileSecrets(host_secrets_path())

    minted_values: dict[str, str] = {}
    cred_entries: list[CredEntry] = []

    for label, cred in credentials.items():
        if cred.source == "secret":
            continue

        provider = reg.get(cred.source)
        if provider is None:
            continue

        minted = provider.mint(label, identity, cred.settings, launch_secrets)

        token_ref = cred_token_ref(label, cred)

        if cred.kind == KIND_TOKEN:
            minted_values[token_ref] = minted.values.get("token", "")

        slots = _build_slots(plan.egress_plan.token_env_map, token_ref, minted)
        cred_entries.append((label, minted, provider, cred.settings, renewal_secrets, slots))

    if not minted_values and not cred_entries:
        return plan, []

    updated = dataclasses.replace(
        plan, minted_cred_values={**plan.minted_cred_values, **minted_values})
    return updated, cred_entries


def _build_slots(
    token_env_map: dict[str, str],
    token_ref: str,
    minted: MintedCredential,
) -> dict[str, tuple[str, ...]]:
    """The `EGRESS_TOKEN_N` slots that carry each of `minted.values`'s fields.

    For `token` kind (one field), every slot whose `token_ref` matches maps to
    the `"token"` field. Other kinds are deferred to later chunks.
    """
    matching = tuple(slot for slot, ref in token_env_map.items() if ref == token_ref)
    if not matching:
        return {}
    return {field: matching for field in minted.values}


def recredential(
    record: Any,
    backend: Any,
    credentials: Any,
) -> None:
    """Re-establish a re-adopted bottle's forwarded host login."""
    from ...backend import adoption  # pylint: disable=import-outside-toplevel
    adopted = adoption.read(record.id)
    if adopted is None or adopted.credentials is None:
        return
    try:
        bottle = backend.attach_adopted(record.id, adopted)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"bottle {record.id}: can't refresh its host login: {e}")
        return
    if bottle is not None:
        credentials.add(record.id, adopted.credentials,
                        registry_id=adopted.registry_id, bottle=bottle)


def recredential_block(
    record: Any,
    backend: Any,
    cred_renewer: Any,
    push: Callable[[str, str, str, str], None],
    secrets_path: Path | None = None,
) -> None:
    """Re-mint a re-adopted bottle's `credentials:` block and register them."""
    # pylint: disable=import-outside-toplevel
    from ...backend import adoption
    from ...credentials import default_registry as _reg
    from ...secret_source import FileSecrets
    from .credential_refresh import read_env_var_secret

    adopted = adoption.read(record.id)
    if adopted is None or adopted.cred_block is None or not adopted.cred_block.entries:
        return
    try:
        bottle = backend.attach_adopted(record.id, adopted)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"bottle {record.id}: can't re-mint its credentials: {e}")
        return
    if bottle is None:
        return
    env_var_secret = read_env_var_secret(bottle)
    reg = _reg()
    secrets = FileSecrets(secrets_path or host_secrets_path())
    cred_entries = []
    for entry in adopted.cred_block.entries:
        provider = reg.get(entry.source)
        if provider is None:
            log.warn(f"bottle {record.id}: credential {entry.label!r}: "
                     f"unknown source {entry.source!r}; skipping re-mint")
            continue
        try:
            minted = provider.mint(entry.label, record.id, entry.settings, secrets)
            for field_name, slot_list in entry.slots.items():
                value = minted.values.get(field_name, "")
                for slot in slot_list:
                    push(adopted.registry_id, slot, value, env_var_secret)
            cred_entries.append((entry.label, minted, provider, entry.settings,
                                 secrets, {k: tuple(v) for k, v in entry.slots.items()}))
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"bottle {record.id}: couldn't re-mint credential {entry.label!r}: {e}")
    if cred_entries:
        cred_renewer.add(record.id, adopted.registry_id, env_var_secret, cred_entries)


def audit_cred_entries(
    cred_entries: "list[CredEntry]",
    audit: Any,
    identity: str,
    activation: str,
) -> None:
    """Emit `credential.minted` audit events for each newly minted entry."""
    for lbl, minted, prov, *_ in cred_entries:
        audit.credential_event(
            "credential.minted", bottle=identity, activation=activation,
            harness="credentials", label=lbl, cred_kind=minted.kind,
            source=prov.source, principal=minted.principal)


__all__ = [
    "CredEntry", "audit_cred_entries", "mint_cred_block",
    "recredential", "recredential_block",
]
