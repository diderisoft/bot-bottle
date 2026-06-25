"""DLP detector-config parsing for egress routes (PRD 0053, PRD 0062).

A route's optional `dlp:` block names which outbound/inbound detectors run
and what the proxy does when an outbound detector matches a token
(`outbound_on_match`). This module owns parsing and validating that block,
kept apart from the request-time scan/decision flow in `egress_addon_core`
so each half reads top-to-bottom without scrolling past the other.

Stdlib-only; ships flat into the sidecar bundle image alongside
`egress_addon_core.py` — see `Dockerfile.sidecars`."""

from __future__ import annotations

import typing

OUTBOUND_DETECTOR_NAMES = frozenset({"token_patterns", "known_secrets", "entropy"})
INBOUND_DETECTOR_NAMES = frozenset({"naive_injection_detection"})

# Per-route policy for what the proxy does when an outbound DLP detector
# matches a token (PRD 0062).
ON_MATCH_BLOCK = "block"          # hard 403, never overridable
ON_MATCH_REDACT = "redact"        # scrub the matched value, forward the request
ON_MATCH_SUPERVISE = "supervise"  # queue for operator approval, hold the request
OUTBOUND_ON_MATCH_VALUES = (ON_MATCH_BLOCK, ON_MATCH_REDACT, ON_MATCH_SUPERVISE)
# Unset resolves to supervise (fall back to block when supervise is not wired).
DEFAULT_OUTBOUND_ON_MATCH = ON_MATCH_SUPERVISE


def parse_dlp_block(
    idx: int,
    host: str,
    raw_dict: dict[str, object],
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, str]:
    """Parse the optional `dlp` block on a route, returning
    (outbound_detectors, inbound_detectors, outbound_on_match)."""
    dlp_raw = raw_dict.get("dlp")
    if dlp_raw is None:
        return None, None, ""
    label = f"route[{idx}] ({host})"
    if not isinstance(dlp_raw, dict):
        raise ValueError(f"{label}: 'dlp' must be an object")
    dlp = typing.cast(dict[str, object], dlp_raw)

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
                f"{label}: dlp.{field} must be false, a list, or omitted"
            )
        items = typing.cast(list[object], val)
        names: list[str] = []
        for j, item in enumerate(items):
            if not isinstance(item, str):
                raise ValueError(
                    f"{label}: dlp.{field}[{j}] must be a string"
                )
            if item not in valid_names:
                raise ValueError(
                    f"{label}: dlp.{field}[{j}] {item!r} is not a valid "
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
                f"{label}: dlp.outbound_on_match must be one of "
                f"{', '.join(OUTBOUND_ON_MATCH_VALUES)} (got {on_match_raw!r})"
            )
        on_match = on_match_raw

    for k in dlp:
        if k not in ("outbound_detectors", "inbound_detectors", "outbound_on_match"):
            raise ValueError(
                f"{label}: dlp has unknown key {k!r}; accepted keys "
                f"are 'outbound_detectors', 'inbound_detectors', "
                f"'outbound_on_match'"
            )
    return outbound, inbound, on_match
