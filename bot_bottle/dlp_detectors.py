"""DLP detectors for the egress proxy (PRD 0053).

Pure Python, no mitmproxy dependency. Each detector is a module-level
function returning `ScanResult | None`.

Ships flat into the sidecar bundle image alongside
`egress_addon_core.py` — both this file and the package source use
the same try/except import shim pattern.
"""

from __future__ import annotations

import base64
import gzip
import re
import typing
import unicodedata
from math import log2
from collections import Counter
from urllib.parse import quote as url_quote

try:
    from egress_addon_core import ScanResult  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - host-side path
    from .egress_addon_core import ScanResult


# ---------------------------------------------------------------------------
# Snippet helpers
# ---------------------------------------------------------------------------

SNIPPET_CONTEXT = 40  # chars of surrounding text to include on each side
REDACT = "********"   # fixed-width replacement for the matched sensitive value


def _snippet(text: str, start: int, end: int) -> str:
    """Return context around a match with the matched span replaced by REDACT."""
    before = text[max(0, start - SNIPPET_CONTEXT):start].replace("\n", " ").replace("\r", " ")
    after = text[end:end + SNIPPET_CONTEXT].replace("\n", " ").replace("\r", " ")
    return f"{before}{REDACT}{after}"


# ---------------------------------------------------------------------------
# Unicode normalization (defeats confusable-char and combining-mark evasion)
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    # NFKD separates base characters from combining marks and resolves
    # compatibility equivalents (fullwidth ASCII, ligatures, etc.)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in decomposed
        # Strip combining marks inserted between chars to break patterns
        if unicodedata.category(ch) != "Mn"
        # Strip control chars; keep common whitespace (\n \r \t)
        and (unicodedata.category(ch) != "Cc" or ch in "\n\r\t")
    )


# ---------------------------------------------------------------------------
# Token patterns detector
# ---------------------------------------------------------------------------

TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token (classic)", re.compile(r"ghp_[A-Za-z0-9_]{36}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{82}")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{93}")),
    ("OpenAI API key", re.compile(r"sk-[A-Za-z0-9]{48}")),
    ("OpenAI project API key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{48,}")),
    ("Stripe live key", re.compile(r"sk_live_[A-Za-z0-9]{24}")),
    ("Generic Bearer JWT", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{50,}")),
    ("HuggingFace token", re.compile(r"hf_[A-Za-z0-9]{34,}")),
    ("Databricks token", re.compile(r"dapi[A-Za-z0-9]{32}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9]+-[A-Za-z0-9]+-[A-Za-z0-9]{24,}")),
    ("npm token", re.compile(r"npm_[A-Za-z0-9]{36}")),
    ("SendGrid API key", re.compile(r"SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}")),
    ("PyPI token", re.compile(r"pypi-[A-Za-z0-9_\-]{80,}")),
    ("HashiCorp Vault token", re.compile(r"hvs\.[A-Za-z0-9_\-]{24,}")),
)


def scan_token_patterns(
    text: str,
    *,
    location: str = "body",
    safe_tokens: typing.AbstractSet[str] | None = None,
) -> ScanResult | None:
    normalized = _normalize_text(text)
    for name, pattern in TOKEN_PATTERNS:
        for m in pattern.finditer(normalized):
            value = m.group(0)
            # A value the supervisor has approved (PRD 0062) is no longer a
            # block — keep scanning so a second, un-approved token in the
            # same request is still caught.
            if safe_tokens is not None and value in safe_tokens:
                continue
            return ScanResult(
                severity="block",
                reason=f"{name} found in {location}",
                location=location,
                context=_snippet(normalized, m.start(), m.end()),
                matched=value,
            )
    return None


def redact_tokens(
    text: str,
    *,
    env: typing.Mapping[str, str] | None = None,
    sensitive_prefixes: tuple[str, ...] = ("EGRESS_TOKEN_",),
) -> str:
    """Replace token pattern matches and (if env given) provisioned secrets with REDACT."""
    for _, pattern in TOKEN_PATTERNS:
        text = pattern.sub(REDACT, text)
    if env is not None:
        for key, value in env.items():
            if any(key.startswith(p) for p in sensitive_prefixes) and value:
                for variant in _encoded_variants(value):
                    text = text.replace(variant, REDACT)
    return text


# ---------------------------------------------------------------------------
# Known secrets detector
# ---------------------------------------------------------------------------

# Encoded-variant cache. Provisioned secrets are stable for the life of the
# proxy, but `_encoded_variants` is on the per-request hot path — it runs for
# every secret on every redaction and known-secret scan (host, path, each
# header, body). Deriving the variant set is relatively expensive (gzip +
# nine encodings), so memoize it per distinct secret. The proxy process
# already holds these values in `os.environ`, so caching them here adds no
# new exposure.
_VARIANT_CACHE: dict[str, tuple[str, ...]] = {}


def _encoded_variants(secret: str) -> list[str]:
    """Return the secret plus common encoded variants for exfil detection.

    The variant set is computed once per distinct secret and cached; callers
    get a fresh list so they can't mutate the shared cached tuple."""
    cached = _VARIANT_CACHE.get(secret)
    if cached is None:
        cached = _compute_encoded_variants(secret)
        _VARIANT_CACHE[secret] = cached
    return list(cached)


def _compute_encoded_variants(secret: str) -> tuple[str, ...]:
    """Derive the secret plus its encoded variants (uncached)."""
    seen: set[str] = {secret}
    variants: list[str] = [secret]

    def _add(v: str) -> None:
        if v not in seen:
            seen.add(v)
            variants.append(v)

    secret_bytes = secret.encode("utf-8")

    # Standard base64 — with and without padding
    b64 = base64.b64encode(secret_bytes).decode("ascii")
    _add(b64)
    _add(b64.rstrip("="))

    # URL-safe base64 (JWT/OAuth use -_ alphabet) — with and without padding
    b64url = base64.urlsafe_b64encode(secret_bytes).decode("ascii")
    _add(b64url)
    _add(b64url.rstrip("="))

    # URL percent-encoding
    _add(url_quote(secret, safe=""))

    # Hex — lowercase and uppercase
    _add(secret_bytes.hex())
    _add(secret_bytes.hex().upper())

    # Base32 (TOTP seeds, some DNS-exfil channels)
    _add(base64.b32encode(secret_bytes).decode("ascii"))

    # gzip + base64 (deterministic: mtime=0); recognisable by H4sI prefix
    _add(base64.b64encode(gzip.compress(secret_bytes, mtime=0)).decode("ascii"))

    return tuple(variants)


# ---------------------------------------------------------------------------
# Fragmentation-resistant helpers
# ---------------------------------------------------------------------------

# Minimum length of alnum projection for projection-based checks to run.
# Short secrets produce too many false positives in projection space.
_ALNUM_MIN_LEN = 8

# Minimum window length for the partial-substring sliding scan.
PARTIAL_MATCH_MIN_LEN = 12


def _alnum_projection(text: str) -> str:
    """Return text with every non-alphanumeric character stripped.

    Used for fragmentation-resistant matching: separator-injected secrets
    (spaces, hyphens, dots inserted between characters) are identical to
    their originals in alnum projection space.
    """
    return "".join(c for c in text if c.isalnum())


def _find_partial_window(secret_alnum: str, text_alnum: str, min_len: int) -> int | None:
    """Return the earliest position in text_alnum holding a min_len-char window
    that also appears in secret_alnum, or None.

    The secret's set of min_len-grams is small (bounded by the secret length),
    so building it once and sweeping the text a single time is O(len(text))
    rather than the O(len(secret) * len(text)) of repeated substring searches —
    which matters because this runs per provisioned secret on every request
    body. Coverage is unchanged: a hit still means at least min_len consecutive
    alphanumeric characters of the secret leaked into the text.
    """
    if len(secret_alnum) < min_len or len(text_alnum) < min_len:
        return None
    secret_grams = {
        secret_alnum[i:i + min_len]
        for i in range(len(secret_alnum) - min_len + 1)
    }
    for pos in range(len(text_alnum) - min_len + 1):
        if text_alnum[pos:pos + min_len] in secret_grams:
            return pos
    return None


def scan_known_secrets(
    text: str,
    *,
    location: str = "body",
    env: typing.Mapping[str, str] | None = None,
    sensitive_prefixes: tuple[str, ...] = ("EGRESS_TOKEN_",),
    safe_tokens: typing.AbstractSet[str] | None = None,
) -> ScanResult | None:
    if env is None:
        return None

    # Pre-compute alnum projection of the scan text once; reused per secret.
    text_alnum: str | None = None

    for key, value in env.items():
        if not any(key.startswith(p) for p in sensitive_prefixes) or not value:
            continue

        # Pass 1: exact match across encoded variants (original behaviour).
        approved_exact = False
        for variant in _encoded_variants(value):
            pos = text.find(variant)
            if pos >= 0:
                # The supervisor approves the exact encoded variant found
                # (PRD 0062); a different encoding of the same secret is a
                # fresh block.
                if safe_tokens is not None and variant in safe_tokens:
                    approved_exact = True
                    continue
                return ScanResult(
                    severity="block",
                    reason=f"provisioned secret from {key} found in {location}",
                    location=location,
                    context=_snippet(text, pos, pos + len(variant)),
                    matched=variant,
                )
        if approved_exact:
            # Exact match was found and approved; projection passes would
            # fire on the same value, so skip them for this secret.
            continue

        # Pass 2 & 3: fragmentation-resistant projection checks.
        secret_alnum = _alnum_projection(value)
        if len(secret_alnum) < _ALNUM_MIN_LEN:
            continue

        if text_alnum is None:
            text_alnum = _alnum_projection(text)

        # Pass 2: full alnum-projection exact match (catches separator injection).
        pos2 = text_alnum.find(secret_alnum)
        if pos2 >= 0:
            return ScanResult(
                severity="block",
                reason=(
                    f"provisioned secret from {key} found in {location} "
                    f"(fragmented match — separator injection)"
                ),
                location=location,
                context=_snippet(text_alnum, pos2, pos2 + len(secret_alnum)),
            )

        # Pass 3: sliding-window partial match (catches chunked-substring leaks).
        pos3 = _find_partial_window(secret_alnum, text_alnum, PARTIAL_MATCH_MIN_LEN)
        if pos3 is not None:
            return ScanResult(
                severity="block",
                reason=(
                    f"provisioned secret from {key} found in {location} "
                    f"(partial match — at least {PARTIAL_MATCH_MIN_LEN} consecutive "
                    f"alphanumeric chars)"
                ),
                location=location,
                context=_snippet(text_alnum, pos3, pos3 + PARTIAL_MATCH_MIN_LEN),
            )

    return None


# ---------------------------------------------------------------------------
# Entropy detector (warn-only)
# ---------------------------------------------------------------------------

# Sliding window size and step for the entropy scan.
ENTROPY_WINDOW = 64
ENTROPY_STEP = 32

# Bits-per-character threshold.  Random ASCII printable ≈ 6.6 bits; random
# lowercase hex ≈ 4 bits; random base64url ≈ 6 bits.  5.5 sits above
# typical structured data (JSON, URLs) while staying below truly random
# content.
ENTROPY_BLOCK_THRESHOLD = 5.5


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum((c / n) * log2(c / n) for c in counts.values())


def scan_entropy(
    text: str,
    *,
    location: str = "body",
    window: int = ENTROPY_WINDOW,
    threshold: float = ENTROPY_BLOCK_THRESHOLD,
) -> ScanResult | None:
    """Warn-only detector: flag windows of `window` chars with Shannon entropy
    above `threshold` bits per character.

    Never blocks; always returns severity='warn'.  Disabled by default —
    routes must opt in via dlp.outbound_detectors=['entropy'].
    """
    if not text:
        return None
    step = max(1, window // 2)
    end = len(text)
    # Scan overlapping windows; also check the final tail if shorter than window.
    positions = list(range(0, end - window + 1, step))
    if end < window:
        positions = [0]
    elif (end - window) % step != 0:
        positions.append(end - window)
    for i in positions:
        chunk = text[i:i + window]
        if _shannon_entropy(chunk) >= threshold:
            return ScanResult(
                severity="warn",
                reason=f"high-entropy content in {location} (possible encrypted exfil)",
                location=location,
                context=_snippet(text, i, i + len(chunk)),
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


PROXIMITY_CHARS = 500


def _closest_pair(
    a_matches: list[re.Match[str]],
    b_matches: list[re.Match[str]],
) -> tuple[re.Match[str], re.Match[str]] | None:
    """Return the pair (a, b) with the smallest character gap, or None."""
    best: tuple[re.Match[str], re.Match[str]] | None = None
    best_gap: int | None = None
    for a in a_matches:
        for b in b_matches:
            gap = max(0, max(a.start(), b.start()) - min(a.end(), b.end()))
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best = (a, b)
    return best


def scan_naive_injection(text: str) -> ScanResult | None:
    location = "response body"
    disclosure_hits = [m for p in DISCLOSURE_PHRASES for m in p.finditer(text)]
    jailbreak_hits = [m for p in JAILBREAK_PHRASES for m in p.finditer(text)]

    if disclosure_hits and jailbreak_hits:
        pair = _closest_pair(disclosure_hits, jailbreak_hits)
        if pair is not None:
            dist = max(0, max(pair[0].start(), pair[1].start()) - min(pair[0].end(), pair[1].end()))
            if dist <= PROXIMITY_CHARS:
                first = pair[0] if pair[0].start() <= pair[1].start() else pair[1]
                return ScanResult(
                    severity="block",
                    reason=(
                        f"disclosure and jailbreak phrases within "
                        f"{dist} chars in {location}"
                    ),
                    location=location,
                    context=_snippet(text, first.start(), first.end()),
                )

    if disclosure_hits:
        m = disclosure_hits[0]
        return ScanResult(
            severity="warn",
            reason=f"prompt disclosure phrase detected in {location}",
            location=location,
            context=_snippet(text, m.start(), m.end()),
        )

    if jailbreak_hits:
        m = jailbreak_hits[0]
        return ScanResult(
            severity="warn",
            reason=f"jailbreak phrase detected in {location}",
            location=location,
            context=_snippet(text, m.start(), m.end()),
        )

    return None


# ---------------------------------------------------------------------------
# CRLF injection detector
# ---------------------------------------------------------------------------

# URL-encoded CRLF is never legitimate in a request URL or header value.
_CRLF_ENCODED_RE = re.compile(r"%0[dD]%0[aA]", re.ASCII)
# Literal CRLF followed by a header-name pattern indicates header injection.
_CRLF_HEADER_INJECT_RE = re.compile(r"\r\n[A-Za-z][A-Za-z0-9\-]+\s*:", re.ASCII)


def strip_crlf(text: str) -> str:
    """Remove URL-encoded and literal CRLF injection sequences from a request
    surface (PRD 0062 redact policy). Used to scrub the request line / headers
    so the request can be forwarded instead of hard-blocked."""
    text = _CRLF_ENCODED_RE.sub("", text)
    return _CRLF_HEADER_INJECT_RE.sub(lambda m: m.group(0)[2:], text)


def scan_crlf_injection(text: str) -> ScanResult | None:
    if _CRLF_ENCODED_RE.search(text):
        return ScanResult(
            severity="block",
            reason="URL-encoded CRLF (%0d%0a) in outbound request",
        )
    if _CRLF_HEADER_INJECT_RE.search(text):
        return ScanResult(
            severity="block",
            reason="CRLF header injection pattern in outbound request",
        )
    return None


__all__ = [
    "ENTROPY_BLOCK_THRESHOLD",
    "ENTROPY_WINDOW",
    "ENTROPY_STEP",
    "PARTIAL_MATCH_MIN_LEN",
    "REDACT",
    "SNIPPET_CONTEXT",
    "TOKEN_PATTERNS",
    "_alnum_projection",
    "_shannon_entropy",
    "redact_tokens",
    "scan_crlf_injection",
    "scan_entropy",
    "scan_known_secrets",
    "scan_naive_injection",
    "scan_token_patterns",
    "strip_crlf",
]
