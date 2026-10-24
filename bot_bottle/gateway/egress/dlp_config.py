"""Parsing of a route's `inspect:` block: which outbound and inbound detectors
run, and `outbound_on_match`."""

from __future__ import annotations

import typing

OUTBOUND_DETECTOR_NAMES = frozenset({"token_patterns", "known_secrets", "entropy"})
INBOUND_DETECTOR_NAMES = frozenset({"naive_injection_detection"})

# What the proxy does when an outbound detector matches a token.
ON_MATCH_BLOCK = "block"  # hard 403
ON_MATCH_REDACT = "redact"  # scrub the value, forward
ON_MATCH_SUPERVISE = "supervise"  # hold for operator approval
OUTBOUND_ON_MATCH_VALUES = (ON_MATCH_BLOCK, ON_MATCH_REDACT, ON_MATCH_SUPERVISE)
# Unset is supervise (block when supervise isn't wired).
DEFAULT_OUTBOUND_ON_MATCH = ON_MATCH_SUPERVISE


def parse_inspect_block(
    idx: int,
    host: str,
    inspect: dict[str, object],
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, str]:
    """Parse DLP settings from an inspected route."""
    label = f"route[{idx}] ({host})"
    dlp = inspect

    def _parse_detector_field(
        field: str,
        valid_names: frozenset[str],
    ) -> tuple[str, ...] | None:
        val = dlp.get(field)
        if val is None:
            return None
        if val is False:
            return ()
        if not isinstance(val, list):
            raise ValueError(
                f"{label}: inspect.{field} must be false, a list, or omitted"
            )
        items = typing.cast(list[object], val)
        names: list[str] = []
        for j, item in enumerate(items):
            if not isinstance(item, str):
                raise ValueError(
                    f"{label}: inspect.{field}[{j}] must be a string"
                )
            if item not in valid_names:
                raise ValueError(
                    f"{label}: inspect.{field}[{j}] {item!r} is not a valid "
                    f"detector name; valid names: {', '.join(sorted(valid_names))}"
                )
            names.append(item)
        return tuple(names)

    outbound = _parse_detector_field("outbound_detectors", OUTBOUND_DETECTOR_NAMES)
    inbound = _parse_detector_field("inbound_detectors", INBOUND_DETECTOR_NAMES)

    on_match = ""
    on_match_raw = dlp.get("outbound_on_match")
    if on_match_raw is not None:
        if not isinstance(on_match_raw, str) or on_match_raw not in OUTBOUND_ON_MATCH_VALUES:
            raise ValueError(
                f"{label}: inspect.outbound_on_match must be one of "
                f"{', '.join(OUTBOUND_ON_MATCH_VALUES)} (got {on_match_raw!r})"
            )
        on_match = on_match_raw

    return outbound, inbound, on_match
