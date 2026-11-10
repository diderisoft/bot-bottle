"""Shared manifest primitives."""

from __future__ import annotations

from typing import cast


class ManifestError(Exception):
    """A manifest file (or the manifest tree) is invalid."""


MANIFEST_DOCS = "docs/reference/manifest.md"


class OldFormError(ManifestError):
    """A manifest still writes something in a form it no longer takes; the
    message names the new form, in one line."""

    def __init__(self, where: str, change: str) -> None:
        self.where = where
        super().__init__(f"{where}: {change} (see {MANIFEST_DOCS})")


def as_json_object(value: object, label: str) -> dict[str, object]:
    """`value` as a `dict[str, object]`; raises unless it's a str-keyed dict."""
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a mapping (was {type(value).__name__})")
    items = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for k, v in items.items():
        if not isinstance(k, str):
            raise ManifestError(f"{label} keys must be strings (found {type(k).__name__})")
        out[k] = v
    return out
