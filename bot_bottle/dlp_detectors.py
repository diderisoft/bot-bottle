"""DLP detectors for the egress proxy (PRD 0053).

Pure Python, no mitmproxy dependency. Each detector is a module-level
function returning `ScanResult | None`.

Ships flat into the sidecar bundle image alongside
`egress_addon_core.py` — both this file and the package source use
the same try/except import shim pattern.
"""

from __future__ import annotations

import base64
import re
import typing
from urllib.parse import quote as url_quote

try:
    from egress_addon_core import ScanResult  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - host-side path
    from .egress_addon_core import ScanResult


# ---------------------------------------------------------------------------
# Token patterns detector (Phase 1a)
# ---------------------------------------------------------------------------

TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token (classic)", re.compile(r"ghp_[A-Za-z0-9_]{36}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{82}")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{93}")),
    ("OpenAI API key", re.compile(r"sk-[A-Za-z0-9]{48}")),
    ("Stripe live key", re.compile(r"sk_live_[A-Za-z0-9]{24}")),
    ("Generic Bearer JWT", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{50,}")),
)


def scan_token_patterns(text: str) -> ScanResult | None:
    for name, pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            return ScanResult(
                severity="block",
                reason=f"outbound request contains {name}",
            )
    return None


# ---------------------------------------------------------------------------
# Known secrets detector (Phase 1b)
# ---------------------------------------------------------------------------

def _encoded_variants(secret: str) -> list[str]:
    """Return the secret plus base64, URL-encoded, and hex variants."""
    variants = [secret]
    secret_bytes = secret.encode("utf-8")
    b64 = base64.b64encode(secret_bytes).decode("ascii")
    if b64 != secret:
        variants.append(b64)
    url_enc = url_quote(secret, safe="")
    if url_enc != secret:
        variants.append(url_enc)
    hex_enc = secret_bytes.hex()
    if hex_enc != secret:
        variants.append(hex_enc)
    return variants


def scan_known_secrets(
    text: str,
    *,
    env: typing.Mapping[str, str] | None = None,
) -> ScanResult | None:
    if env is None:
        return None
    for key, value in env.items():
        if not key.startswith("EGRESS_TOKEN_") or not value:
            continue
        for variant in _encoded_variants(value):
            if variant in text:
                return ScanResult(
                    severity="block",
                    reason=(
                        f"outbound request contains provisioned secret "
                        f"from {key}"
                    ),
                )
    return None


# ---------------------------------------------------------------------------
# Naive prompt injection detector (Phase 2)
# ---------------------------------------------------------------------------

DISCLOSURE_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)system\s+prompt"),
    re.compile(r"(?i)my\s+instructions\s+are"),
    re.compile(r"(?i)original\s+instructions"),
    re.compile(r"(?i)secret\s+instructions"),
    re.compile(r"(?i)hidden\s+rules"),
)

JAILBREAK_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+previous"),
    re.compile(r"(?i)forget\s+everything"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior)"),
    re.compile(r"(?i)pretend\s+you\s+are"),
    re.compile(r"(?i)act\s+as\s+(?:if|though)"),
)


def scan_naive_injection(text: str) -> ScanResult | None:
    disclosure = any(p.search(text) for p in DISCLOSURE_PHRASES)
    token = scan_token_patterns(text) is not None

    # Tier 1: credential + disclosure = BLOCK
    if disclosure and token:
        return ScanResult(
            severity="block",
            reason="prompt disclosure with embedded credential in response",
        )

    # Tier 2: multiple jailbreak phrases = WARN
    jailbreak_count = sum(1 for p in JAILBREAK_PHRASES if p.search(text))
    if jailbreak_count >= 2:
        return ScanResult(
            severity="warn",
            reason=f"{jailbreak_count} jailbreak phrases detected in response",
        )

    # Tier 2b: explicit prompt disclosure without credential = WARN
    if disclosure and "system prompt:" in text.lower():
        return ScanResult(
            severity="warn",
            reason="explicit system prompt disclosure in response",
        )

    return None


__all__ = [
    "TOKEN_PATTERNS",
    "scan_known_secrets",
    "scan_naive_injection",
    "scan_token_patterns",
]
