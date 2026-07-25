"""Shared manifest primitives used by all manifest sub-modules."""

from __future__ import annotations

from typing import cast


class ManifestError(Exception):
    """A manifest file (or the manifest tree) is invalid."""


def as_json_object(value: object, label: str) -> dict[str, object]:
    """Assert that `value` is a JSON object (str-keyed dict) and return
    a view typed as `dict[str, object]` so downstream `.get(...)` calls
    have a typed surface."""
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a JSON object (was {type(value).__name__})")
    items = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for k, v in items.items():
        if not isinstance(k, str):
            raise ManifestError(f"{label} keys must be strings (found {type(k).__name__})")
        out[k] = v
    return out
