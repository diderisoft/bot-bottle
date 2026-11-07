"""DLP detectors for the egress proxy. Pure Python; each returns
`ScanResult | None`.
"""

from __future__ import annotations

import base64
import functools
import gzip
import re
import typing
import unicodedata
from math import log2
from collections import Counter
from urllib.parse import quote as url_quote

from .types import ScanResult


# --- Snippets ---

SNIPPET_CONTEXT = 40  # chars of context on each side
REDACT = "********"  # fixed-width replacement


def _snippet(text: str, start: int, end: int) -> str:
    """Context around a match, with the match replaced by REDACT."""
    before = text[max(0, start - SNIPPET_CONTEXT):start].replace("\n", " ").replace("\r", " ")
    after = text[end:end + SNIPPET_CONTEXT].replace("\n", " ").replace("\r", " ")
    return f"{before}{REDACT}{after}"


# --- Unicode normalization (confusables, combining marks) ---

def _normalize_text(text: str) -> str:
    # NFKD splits off combining marks and folds compatibility forms.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in decomposed
        # Strip combining marks.
        if unicodedata.category(ch) != "Mn"
        # Strip control chars except \n \r \t.
        and (unicodedata.category(ch) != "Cc" or ch in "\n\r\t")
    )


# --- Token patterns ---

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
            # An approved value no longer blocks; keep scanning for others.
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
    """Replace token-pattern matches and (with `env`) provisioned secrets with
    REDACT."""
    for _, pattern in TOKEN_PATTERNS:
        text = pattern.sub(REDACT, text)
    if env is not None:
        for key, value in env.items():
            if any(key.startswith(p) for p in sensitive_prefixes) and value:
                for variant in _encoded_variants(value):
                    text = text.replace(variant, REDACT)
    return text


# --- Known secrets ---

# Memoized per secret: deriving variants (gzip plus nine encodings) is costly
# and runs per secret, per surface, per request. Bounded.
_VARIANT_CACHE_MAXSIZE = 256


def _encoded_variants(secret: str) -> list[str]:
    """The secret and its common encodings, cached; returns a fresh list."""
    return list(_compute_encoded_variants(secret))


@functools.lru_cache(maxsize=_VARIANT_CACHE_MAXSIZE)
def _compute_encoded_variants(secret: str) -> tuple[str, ...]:
    """The secret and its encodings (memoized, bounded)."""
    seen: set[str] = {secret}
    variants: list[str] = [secret]

    def _add(v: str) -> None:
        if v not in seen:
            seen.add(v)
            variants.append(v)

    secret_bytes = secret.encode("utf-8")

    # base64, with and without padding
    b64 = base64.b64encode(secret_bytes).decode("ascii")
    _add(b64)
    _add(b64.rstrip("="))

    # URL-safe base64, with and without padding
    b64url = base64.urlsafe_b64encode(secret_bytes).decode("ascii")
    _add(b64url)
    _add(b64url.rstrip("="))

    # percent-encoding
    _add(url_quote(secret, safe=""))

    # hex, both cases
    _add(secret_bytes.hex())
    _add(secret_bytes.hex().upper())

    # base32
    _add(base64.b32encode(secret_bytes).decode("ascii"))

    # gzip + base64 (mtime=0, deterministic)
    _add(base64.b64encode(gzip.compress(secret_bytes, mtime=0)).decode("ascii"))

    return tuple(variants)


# --- Fragmentation-resistant helpers ---

# Minimum alnum-projection length; shorter secrets false-positive.
_ALNUM_MIN_LEN = 8

# Minimum consecutive alphanumeric characters of a secret that must appear in
# the outbound text to trigger a partial-match block.  Set to 12 because:
#   - Every TOKEN_PATTERNS entry has an alnum payload ≥ 16 chars (the shortest
#     is `dapi` + 32 alphanumeric = 36 chars), so a 12-char window catches
#     partial leaks while staying well above common English words (≤ 7 chars).
#   - Values below ~10 produce noisy false positives on natural prose and JSON.
#   - Values above 15 risk missing heavily fragmented leaks of medium-length keys.
# If a new token class with a shorter key is added, lower this threshold and
# add a targeted test.
PARTIAL_MATCH_MIN_LEN = 12


def _alnum_projection(text: str) -> str:
    """`text` with every non-alphanumeric character removed, so
    separator-injected secrets match their originals.
    """
    return "".join(c for c in text if c.isalnum())


def _find_partial_window(secret_alnum: str, text_alnum: str, min_len: int) -> int | None:
    """The earliest position in `text_alnum` of a `min_len` window that also
    appears in `secret_alnum`, or None. Builds the secret's n-grams once for a
    single O(len(text)) sweep.
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

    # Project the text once; reused per secret.
    text_alnum: str | None = None

    for key, value in env.items():
        if not any(key.startswith(p) for p in sensitive_prefixes) or not value:
            continue

        # Pass 1: exact match over encoded variants.
        approved_exact = False
        for variant in _encoded_variants(value):
            pos = text.find(variant)
            if pos >= 0:
                # Approval covers the exact variant found; another encoding is
                # a fresh block.
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
            # Approved exact match: skip the projection passes for this secret.
            continue

        # Passes 2 and 3: projection checks.
        secret_alnum = _alnum_projection(value)
        if len(secret_alnum) < _ALNUM_MIN_LEN:
            continue

        if text_alnum is None:
            text_alnum = _alnum_projection(text)

        # Pass 2: full projection match (separator injection).
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

        # Pass 3: sliding-window partial match (chunked leaks).
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


# --- Entropy (warn-only) ---

# Window size and step.
ENTROPY_WINDOW = 64
ENTROPY_STEP = 32

# Bits per char: random printable ~6.6, hex ~4, base64url ~6. 5.5 sits above
# structured data.
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
    """Warn-only: flag `window`-char windows above `threshold` bits per char.
    Off unless a route lists `entropy`.
    """
    if not text:
        return None
    step = max(1, window // 2)
    end = len(text)
    # Overlapping windows, plus the tail.
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


# --- Naive prompt injection ---

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


def _match_gap(a: re.Match[str], b: re.Match[str]) -> int:
    """Character gap between two spans; 0 when they overlap or touch."""
    return max(0, max(a.start(), b.start()) - min(a.end(), b.end()))


def _closest_pair(
    a_matches: list[re.Match[str]],
    b_matches: list[re.Match[str]],
    *,
    within: int | None = None,
) -> tuple[re.Match[str], re.Match[str]] | None:
    """The closest (a, b) pair, or None if either list is empty. A sorted
    two-pointer sweep instead of the cross product, since the matches are
    attacker-controlled. With `within`, returns at the first pair that close.
    """
    if not a_matches or not b_matches:
        return None
    a_sorted = sorted(a_matches, key=lambda m: m.start())
    b_sorted = sorted(b_matches, key=lambda m: m.start())
    i = j = 0
    best: tuple[re.Match[str], re.Match[str]] | None = None
    best_gap: int | None = None
    while i < len(a_sorted) and j < len(b_sorted):
        a, b = a_sorted[i], b_sorted[j]
        gap = _match_gap(a, b)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = (a, b)
            if within is not None and gap <= within:
                return best
        # Advance whichever span ends first.
        if a.end() <= b.end():
            i += 1
        else:
            j += 1
    return best


def scan_naive_injection(text: str) -> ScanResult | None:
    location = "response body"
    disclosure_hits = [m for p in DISCLOSURE_PHRASES for m in p.finditer(text)]
    jailbreak_hits = [m for p in JAILBREAK_PHRASES for m in p.finditer(text)]

    if disclosure_hits and jailbreak_hits:
        pair = _closest_pair(disclosure_hits, jailbreak_hits, within=PROXIMITY_CHARS)
        if pair is not None:
            dist = _match_gap(pair[0], pair[1])
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


# --- CRLF injection ---

# URL-encoded CRLF is never legitimate in a URL or header.
_CRLF_ENCODED_RE = re.compile(r"%0[dD]%0[aA]", re.ASCII)
# CRLF followed by a header name is header injection.
_CRLF_HEADER_INJECT_RE = re.compile(r"\r\n[A-Za-z][A-Za-z0-9\-]+\s*:", re.ASCII)


def strip_crlf(text: str) -> str:
    """Remove encoded and literal CRLF sequences from a request surface, so it
    can be forwarded rather than blocked."""
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
