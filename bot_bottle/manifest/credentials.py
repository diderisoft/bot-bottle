"""ManifestCredential: parsing the `credentials:` block.

A credential is a label, a source, its settings, and a kind. The source names
how it's made; the kind names what it is. When a source makes only one kind the
kind is implied, so `kind:` is usually omitted.

`$label` refs in `env:` and `auth.value` point here. This module also exposes
`parse_cred_label` for callers that need to detect a `$label` reference, and
`check_cred_refs` to validate them after both `credentials:` and `env:`/`egress`
are parsed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..credentials import (
    ENV_KINDS, KIND_TOKEN,
    KNOWN_KINDS, CredentialRegistry, default_registry,
)
from .util import ManifestError, as_json_object

# `$label`: a bare `$` followed by a credential label (kebab-case, same
# pattern as bottle names). `${VAR}` (braced) is a host-env ref and is
# handled by `secret_ref`; `$$` is a literal `$` and is stripped by callers.
_CRED_LABEL_RE = re.compile(r"^\$([a-z][a-z0-9-]*)$")

# Source names whose `kind` is implied (they mint exactly one kind).
_IMPLIED_KINDS: dict[str, str] = {
    "prompt": KIND_TOKEN,
    "host_login": KIND_TOKEN,
    "assume_role": "aws",
    "profile": "aws",
    "file": "ssh-key",
    "gitea_deploy_key": "ssh-key",
    # `secret` can mint any kind, so its kind is never implied.
}

# Settings accepted for each built-in source (others fail at load).
_SOURCE_SETTINGS: dict[str, frozenset[str]] = {
    "secret": frozenset({"kind", "secret", "access_key_id", "secret_access_key",
                         "session_token", "private_key", "public_key"}),
    "prompt": frozenset({"message"}),
    "host_login": frozenset({"harness"}),
    # Remaining sources are future chunks; their settings are unchecked in
    # chunk 1 to avoid breaking manifests written against the PRD.
}


def parse_cred_label(value: str) -> str:
    """The credential label if `value` is `$label`, else ''."""
    m = _CRED_LABEL_RE.match(value)
    return m.group(1) if m else ""


def is_cred_ref(value: str) -> bool:
    """True if `value` is a `$label` credential reference."""
    return bool(_CRED_LABEL_RE.match(value))


def unescape_dollar(value: str) -> str:
    """Replace a leading `$$` with a literal `$`. Other `$$` are not special."""
    if value.startswith("$$"):
        return value[1:]
    return value


@dataclass(frozen=True)
class ManifestCredential:
    """One entry under `credentials:`."""

    label: str
    source: str
    # Resolved kind; never empty after `from_dict`.
    kind: str
    # Raw settings dict (source-specific keys and values).
    settings: Mapping[str, object]
    # `agent_can_read: true` lets the agent pull the real value via a helper.
    # Chunk 1: always False (the field is validated and rejected in project
    # bottles, but accepted in host bottles for forward-compatibility).
    agent_can_read: bool = False

    @classmethod
    def from_dict(
        cls,
        label: str,
        bottle: str,
        raw: object,
        *,
        project_bottle: bool = False,
        registry: CredentialRegistry | None = None,
    ) -> "ManifestCredential":
        ctx = f"bottle '{bottle}' credentials.{label}"
        d = as_json_object(raw, ctx)
        reg = registry or default_registry()

        # --- source ---
        source_raw = d.get("source")
        if not isinstance(source_raw, str) or not source_raw:
            raise ManifestError(f"{ctx}: 'source' is required")
        source = source_raw

        provider = reg.get(source)
        if provider is None:
            known = ", ".join(sorted(reg.known_sources()))
            raise ManifestError(
                f"{ctx}: unknown source {source!r}; "
                f"built-in sources are {known}"
            )

        # --- project_definable ---
        if project_bottle and not provider.project_definable:
            raise ManifestError(
                f"{ctx}: source {source!r} may not be used in a project bottle"
            )

        # --- kind ---
        implied = _IMPLIED_KINDS.get(source)
        kind_raw = d.get("kind")
        if kind_raw is None:
            if len(provider.kinds) == 1:
                kind = provider.kinds[0]
            elif implied is not None:
                kind = implied
            elif source == "secret":
                kind = KIND_TOKEN  # default for secret source
            else:
                kinds_str = ", ".join(provider.kinds)
                raise ManifestError(
                    f"{ctx}: 'kind' is required for source {source!r}; "
                    f"one of {kinds_str}"
                )
        else:
            if not isinstance(kind_raw, str):
                raise ManifestError(f"{ctx}: 'kind' must be a string")
            kind = kind_raw
            if kind not in KNOWN_KINDS:
                raise ManifestError(
                    f"{ctx}: unknown kind {kind!r}; "
                    f"one of {', '.join(sorted(KNOWN_KINDS))}"
                )
            if kind not in provider.kinds:
                raise ManifestError(
                    f"{ctx}: source {source!r} cannot mint kind {kind!r}; "
                    f"it mints {', '.join(provider.kinds)}"
                )

        # --- agent_can_read ---
        acr_raw = d.get("agent_can_read", False)
        if not isinstance(acr_raw, bool):
            raise ManifestError(f"{ctx}: 'agent_can_read' must be true or false")
        if acr_raw and project_bottle:
            raise ManifestError(
                f"{ctx}: 'agent_can_read' may not be set in a project bottle"
            )

        # --- settings (source-specific) ---
        allowed = _SOURCE_SETTINGS.get(source)
        settings_keys = frozenset(d) - {"source", "kind", "agent_can_read"}
        if allowed is not None:
            unknown = settings_keys - allowed
            if unknown:
                raise ManifestError(
                    f"{ctx}: unknown setting(s) {sorted(unknown)} for source {source!r}"
                )

        settings: dict[str, object] = {
            k: v for k, v in d.items()
            if k not in ("source", "kind", "agent_can_read")
        }

        # Source-specific required settings.
        if source == "secret":
            _check_secret_settings(ctx, kind, settings)

        return cls(
            label=label, source=source, kind=kind,
            settings=settings, agent_can_read=acr_raw,
        )


def parse_credentials_block(
    bottle: str,
    raw: object,
    *,
    project_bottle: bool = False,
    registry: CredentialRegistry | None = None,
) -> dict[str, ManifestCredential]:
    """Parse `credentials:` → `{label: ManifestCredential}`.

    Labels are kebab-case (the same pattern as bottle names). An empty block
    is fine; None is treated as absent.
    """
    if raw is None:
        return {}
    d = as_json_object(raw, f"bottle '{bottle}' credentials")
    out: dict[str, ManifestCredential] = {}
    for label, entry in d.items():
        if not re.match(r"^[a-z][a-z0-9-]*$", label):
            raise ManifestError(
                f"bottle '{bottle}' credentials: label {label!r} must be "
                f"kebab-case (a-z, 0-9, hyphen, starting with a letter)"
            )
        out[label] = ManifestCredential.from_dict(
            label, bottle, entry,
            project_bottle=project_bottle,
            registry=registry,
        )
    return out


def check_cred_refs(
    bottle: str,
    credentials: Mapping[str, ManifestCredential],
    env_cred_vars: Mapping[str, str],
    route_cred_labels: frozenset[str],
    git_key_cred_labels: frozenset[str],
) -> None:
    """Cross-validate `$label` references after all blocks are parsed.

    - Every `$label` names an existing credential.
    - Kind checks: `env` and bearer/token/header auth take `token` credentials;
      `aws-sigv4` auth takes `aws` credentials; git-gate keys take `ssh-key`.
    - Unswappable placeholder: a `$label` in `env:` must have at least one
      egress route or git-gate key that uses the same credential (otherwise
      the placeholder the agent holds has nowhere to be swapped).
    """
    all_labels = (
        frozenset(env_cred_vars.values())
        | route_cred_labels
        | git_key_cred_labels
    )
    for label in all_labels:
        if label not in credentials:
            raise ManifestError(
                f"bottle '{bottle}': credential {label!r} is used but not "
                f"declared in 'credentials:'"
            )

    # Kind checks for env vars.
    for var, label in env_cred_vars.items():
        cred = credentials[label]
        if cred.kind not in ENV_KINDS:
            raise ManifestError(
                f"bottle '{bottle}' env {var}: credential {label!r} is kind "
                f"{cred.kind!r}; env vars only accept {', '.join(sorted(ENV_KINDS))}"
            )

    # Kind checks for auth routes (checked in egress.py at parse time using
    # the auth scheme; bearer/token/header need token, aws-sigv4 needs aws).
    # Route-level kind checks are done there to keep context richer.

    # Kind checks for git-gate keys.
    for label in git_key_cred_labels:
        cred = credentials[label]
        if cred.kind != "ssh-key":
            raise ManifestError(
                f"bottle '{bottle}' git_gate.repos key: credential {label!r} is "
                f"kind {cred.kind!r}; git-gate keys require kind 'ssh-key'"
            )

    # Unswappable placeholder check: every env credential must be used in
    # at least one egress route or git-gate key so the gateway can swap it.
    swappable = route_cred_labels | git_key_cred_labels
    for var, label in env_cred_vars.items():
        if label not in swappable:
            raise ManifestError(
                f"bottle '{bottle}' env {var}: credential {label!r}'s placeholder "
                f"is never swapped — add an egress route that uses {label!r} in "
                f"its auth, or remove it from env"
            )


def _check_secret_settings(ctx: str, kind: str, settings: Mapping[str, object]) -> None:
    if kind == KIND_TOKEN:
        if "secret" not in settings:
            raise ManifestError(
                f"{ctx}: source 'secret' with kind 'token' requires 'secret: NAME'"
            )
        name = settings["secret"]
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{ctx}: 'secret' must be a non-empty string")
    elif kind == "aws":
        for field in ("access_key_id", "secret_access_key"):
            val = settings.get(field)
            if not isinstance(val, str) or not val:
                raise ManifestError(
                    f"{ctx}: source 'secret' with kind 'aws' requires "
                    f"'{field}: NAME' (a non-empty string)"
                )
    elif kind == "ssh-key":
        for field in ("private_key", "public_key"):
            val = settings.get(field)
            if not isinstance(val, str) or not val:
                raise ManifestError(
                    f"{ctx}: source 'secret' with kind 'ssh-key' requires "
                    f"'{field}: NAME' (a non-empty string)"
                )


__all__ = [
    "ManifestCredential",
    "check_cred_refs",
    "is_cred_ref",
    "parse_cred_label",
    "parse_credentials_block",
    "unescape_dollar",
]
