"""Compatibility exports for the egress addon's pure logic; new code imports
`types`, `schema`, `context`, `matching` and `dlp` directly.
"""

from .context import (
    DENY_RESOLVER_ERROR,
    DENY_UNATTRIBUTED,
    DENY_UNPARSEABLE,
    ContextResolverLike,
    PolicyResolverLike,
    resolve_client_config,
    resolve_client_context,
)
from .dlp import (
    build_inbound_scan_text,
    build_outbound_scan_text,
    build_token_allow_payload,
    outbound_scan_headers,
    scan_inbound,
    scan_outbound,
)
from .dlp_config import (
    DEFAULT_OUTBOUND_ON_MATCH,
    INBOUND_DETECTOR_NAMES,
    ON_MATCH_BLOCK,
    ON_MATCH_REDACT,
    ON_MATCH_SUPERVISE,
    OUTBOUND_DETECTOR_NAMES,
    OUTBOUND_ON_MATCH_VALUES,
    parse_inspect_block,
)
from .matching import (
    decide,
    decide_git_fetch,
    evaluate_matches,
    is_git_fetch_request,
    is_git_push_request,
    match_route,
)
from .schema import load_config, parse_config, parse_routes, route_to_yaml_dict
from .types import (
    LOG_BLOCKS,
    LOG_FULL,
    LOG_OFF,
    Config,
    Decision,
    HeaderMatch,
    MatchEntry,
    PathMatch,
    Route,
    ScanResult,
)

__all__ = [
    "LOG_BLOCKS", "LOG_FULL", "LOG_OFF",
    "ON_MATCH_BLOCK", "ON_MATCH_REDACT", "ON_MATCH_SUPERVISE",
    "OUTBOUND_ON_MATCH_VALUES", "DEFAULT_OUTBOUND_ON_MATCH",
    "OUTBOUND_DETECTOR_NAMES", "INBOUND_DETECTOR_NAMES",
    "Config", "Decision", "HeaderMatch", "MatchEntry", "PathMatch", "Route",
    "ScanResult", "PolicyResolverLike", "ContextResolverLike",
    "DENY_UNATTRIBUTED", "DENY_UNPARSEABLE", "DENY_RESOLVER_ERROR",
    "build_inbound_scan_text", "build_outbound_scan_text",
    "build_token_allow_payload", "decide", "decide_git_fetch",
    "evaluate_matches", "is_git_push_request", "is_git_fetch_request",
    "load_config", "match_route", "outbound_scan_headers", "parse_config",
    "parse_inspect_block", "parse_routes", "resolve_client_config",
    "resolve_client_context", "route_to_yaml_dict", "scan_inbound",
    "scan_outbound",
]
