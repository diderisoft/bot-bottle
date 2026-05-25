"""Hand-rolled YAML-subset parser for claude-bottle manifest files
(PRD 0011).

Why hand-rolled: the configs we accept have a bounded shape (flat
top-level keys; values are strings / ints / bools / null / lists /
nested dicts; no anchors, no multi-line block scalars, no tags, no
implicit type coercion gotchas). A real YAML library is a much
larger dependency surface than we need. The project's stdlib-only
stance (CLAUDE.md) is the load-bearing reason; the safety
properties — no Norway problem, no surprise date/octal coercion —
are the bonus.

Public API:

    parse_yaml_subset(text) -> dict[str, object]
        Parse a full document. Top level must be a mapping (the
        shape every claude-bottle manifest file uses). Values are
        str / int / bool / None / list / dict only.

    parse_frontmatter(text) -> tuple[dict[str, object], str]
        For a Markdown file with YAML frontmatter delimited by `---`
        lines. Returns (frontmatter_dict, body_text).

What we accept (block-style):

    key: value                # mapping entry, value is inline
    key:                      # mapping entry, value is block
      nested_key: value

    key:
      - item                  # list under a key
      - item

    key:
      - subkey: value1        # list item that's a mapping
        subkey2: value2
      - subkey: value3

What we accept (inline, scalar leaves only):

    key: [a, b, "c d"]
    key: {a: 1, b: 2}

What we reject (each dies with a clear pointer):

    &anchor / *alias          # anchors / aliases
    !!tag                     # YAML tags
    | / >                     # multi-line block scalars
    yes / no / on / off       # only true / false count as bool
    ambiguous bare strings    # numbers, dates, etc. when unquoted
    tabs as indentation       # spaces only
    flow-style nested deeper than one level

Errors carry the line number from the source document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .log import die


# --- Tokenizer / line preprocessing ----------------------------------------


@dataclass(frozen=True)
class _Line:
    """One non-blank, non-comment line from the source. `indent` is
    the column of the first non-space character; `content` is the
    line text from that column onward, with trailing whitespace and
    trailing `# ...` comments stripped. `lineno` is the 1-based
    line in the original document."""

    indent: int
    content: str
    lineno: int


def _strip_trailing_comment(s: str) -> str:
    """Strip ` # comment` from end of line, but only when the `#`
    isn't inside a quoted string. Returns the cleaned line."""
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # `#` must be preceded by whitespace to be a comment,
            # otherwise it's just a literal character.
            if i == 0 or s[i - 1] in (" ", "\t"):
                return s[:i].rstrip()
    return s.rstrip()


def _tokenize(text: str) -> list[_Line]:
    """Drop blank / comment lines, parse indent + content for the
    rest. Tabs in the indent area are rejected outright."""
    out: list[_Line] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        # Tabs in indent are a portability footgun — different
        # editors render them differently and the spec says spaces.
        leading = len(raw) - len(raw.lstrip(" \t"))
        if "\t" in raw[:leading]:
            die(f"yaml-subset: tab character in indent on line {n}")
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # Whole-line position: indent before first non-space.
        indent = len(raw) - len(raw.lstrip(" "))
        content = _strip_trailing_comment(raw[indent:])
        if not content:
            continue
        out.append(_Line(indent=indent, content=content, lineno=n))
    return out


# --- Scalar parsing ---------------------------------------------------------


_BARE_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_INT_RX = re.compile(r"^-?[0-9]+$")
_RESERVED_BOOL_LIKE = frozenset({"yes", "no", "on", "off", "y", "n", "Y", "N",
                                 "YES", "NO", "ON", "OFF", "True", "False",
                                 "TRUE", "FALSE"})
# Yaml-ish ambiguity sources that an unquoted bare token COULD be
# mistaken for: dates, octals, etc. Detected and rejected so users
# quote their strings explicitly. We don't try to enumerate every
# ambiguity; the rule is "if it looks like a non-string literal,
# either parse it as that literal (true/false/null/int) or reject
# it with a 'quote it' hint."
_DATE_RX = re.compile(r"^-?\d{4}-\d{2}-\d{2}(T\d.*)?$")
_OCTAL_RX = re.compile(r"^0o?\d+$")
_HEX_RX = re.compile(r"^0x[0-9A-Fa-f]+$")
_FLOAT_RX = re.compile(r"^-?\d+\.\d+([eE][-+]?\d+)?$")


def _parse_scalar(s: str, lineno: int) -> object:
    """Turn a stripped value string into a Python value (str, int,
    bool, None). Quoted strings preserve their literal content
    (with standard escapes); bare strings are accepted only when
    they're unambiguous."""
    s = s.strip()
    if not s:
        return ""

    # Quoted forms first — content is whatever's between the quotes
    # with the documented escapes applied.
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        if len(s) < 2:
            die(f"yaml-subset: unterminated quoted string on line {lineno}")
        body = s[1:-1]
        if s.startswith('"'):
            # JSON-style escapes for double quotes.
            try:
                return body.encode("utf-8").decode("unicode_escape")
            except UnicodeDecodeError as e:
                die(f"yaml-subset: bad escape on line {lineno}: {e}")
        else:
            # Single quotes: only '' → ' (standard YAML); no other escapes.
            return body.replace("''", "'")

    # Reserved bool-like tokens that aren't `true` / `false` —
    # always reject so users have to be explicit.
    if s in _RESERVED_BOOL_LIKE:
        if s in ("true", "false"):
            return s == "true"
        die(
            f"yaml-subset: bare {s!r} on line {lineno} is ambiguous "
            f"(use literal `true` / `false`, or quote it as a string)"
        )

    if s == "true":
        return True
    if s == "false":
        return False
    if s in ("null", "~"):
        return None

    if _INT_RX.match(s):
        return int(s)

    # Look-alikes that we reject to keep the user in control.
    if _DATE_RX.match(s):
        die(
            f"yaml-subset: bare {s!r} on line {lineno} looks like a "
            f"date — quote it as a string or use an explicit int"
        )
    if _OCTAL_RX.match(s):
        die(
            f"yaml-subset: bare {s!r} on line {lineno} looks like an "
            f"octal/0-prefixed integer — quote it as a string"
        )
    if _HEX_RX.match(s):
        die(
            f"yaml-subset: bare {s!r} on line {lineno} looks like a "
            f"hex integer — quote it as a string"
        )
    if _FLOAT_RX.match(s):
        die(
            f"yaml-subset: floats not supported (line {lineno}, "
            f"value {s!r}); use an int or quote as a string"
        )

    # Bare strings: anything that matches the bare-string pattern is
    # accepted as a string literal. Otherwise we hand it back as a
    # string anyway — for URLs, paths, hostnames, etc. that contain
    # special chars. The PRD calls for rejecting "ambiguous" strings,
    # and we've already rejected the ambiguous shapes above; what's
    # left is unambiguously a string.
    return s


# --- Inline list / dict ----------------------------------------------------


def _parse_inline(s: str, lineno: int) -> object:
    """Inline list `[a, b]` or dict `{a: 1, b: 2}` or scalar.
    Nested flow more than one level deep is rejected (PRD)."""
    s = s.strip()
    if s.startswith("["):
        if not s.endswith("]"):
            die(f"yaml-subset: unterminated `[` on line {lineno}")
        body = s[1:-1].strip()
        if not body:
            return []
        items: list[object] = []
        for raw in _split_flow(body, lineno, "list"):
            v = _parse_scalar(raw, lineno)
            items.append(v)
        return items
    if s.startswith("{"):
        if not s.endswith("}"):
            die(f"yaml-subset: unterminated `{{` on line {lineno}")
        body = s[1:-1].strip()
        if not body:
            return {}
        out: dict[str, object] = {}
        for raw in _split_flow(body, lineno, "dict"):
            if ":" not in raw:
                die(
                    f"yaml-subset: inline dict entry on line {lineno} "
                    f"missing `:` ({raw!r})"
                )
            k, _, v = raw.partition(":")
            k = k.strip()
            if not _BARE_RX.match(k):
                die(
                    f"yaml-subset: inline dict key on line {lineno} "
                    f"must be a bare identifier ({k!r})"
                )
            out[k] = _parse_scalar(v.strip(), lineno)
        return out
    return _parse_scalar(s, lineno)


def _split_flow(body: str, lineno: int, kind: str) -> list[str]:
    """Split `a, b, c` respecting quoted strings. Rejects nested
    flow (a list/dict inside the flow body) since the PRD limits
    flow nesting to one level."""
    items: list[str] = []
    depth_b = 0
    depth_c = 0
    in_single = False
    in_double = False
    cur = []
    for ch in body:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in "[{":
                depth_b += 1
            elif ch in "]}":
                depth_b -= 1
            if depth_b > 0:
                die(
                    f"yaml-subset: nested flow {kind} on line "
                    f"{lineno} (only one level of flow allowed)"
                )
            if ch == "," and depth_b == 0 and depth_c == 0:
                items.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    if cur:
        items.append("".join(cur))
    return [s.strip() for s in items if s.strip()]


# --- Block parser ----------------------------------------------------------


def _split_key_value(content: str, lineno: int) -> tuple[str, str]:
    """Find the FIRST top-level `:` that separates a key from its
    value (ignoring `:` inside quoted strings). Returns (key, value).
    `value` may be empty (block-form mapping)."""
    in_single = False
    in_double = False
    for i, ch in enumerate(content):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            # `:` must be followed by space or be at end-of-line to
            # count as a key separator (otherwise `key:value` would
            # ambiguous with URLs etc.).
            if i + 1 >= len(content) or content[i + 1] in (" ", "\t"):
                return content[:i].strip(), content[i + 1:].lstrip()
    die(f"yaml-subset: line {lineno} missing `: ` separator: {content!r}")


def _parse_block(
    lines: list[_Line], idx: int, base_indent: int
) -> tuple[object, int]:
    """Parse a block starting at `lines[idx]`, expecting that block
    to live at `base_indent`. Returns (value, new_idx) where
    `new_idx` is the index of the first unconsumed line."""
    if idx >= len(lines):
        die("yaml-subset: unexpected end of document")
    first = lines[idx]
    if first.indent < base_indent:
        die(
            f"yaml-subset: line {first.lineno} indented less than "
            f"expected (got {first.indent}, expected >= {base_indent})"
        )
    if first.indent > base_indent:
        die(
            f"yaml-subset: line {first.lineno} indented more than "
            f"expected (got {first.indent}, expected {base_indent})"
        )

    if first.content.startswith("- ") or first.content == "-":
        return _parse_block_list(lines, idx, base_indent)
    return _parse_block_mapping(lines, idx, base_indent)


def _parse_block_mapping(
    lines: list[_Line], idx: int, base_indent: int
) -> tuple[dict[str, object], int]:
    out: dict[str, object] = {}
    while idx < len(lines) and lines[idx].indent == base_indent:
        line = lines[idx]
        if line.content.startswith("- "):
            die(
                f"yaml-subset: line {line.lineno} unexpected list "
                f"item at mapping indent (got `-`, expected `key:`)"
            )
        key, value_text = _split_key_value(line.content, line.lineno)
        if not _BARE_RX.match(key):
            die(
                f"yaml-subset: line {line.lineno} key {key!r} is not "
                f"a bare identifier"
            )
        if key in out:
            die(
                f"yaml-subset: line {line.lineno} duplicate key {key!r}"
            )
        if value_text:
            out[key] = _parse_inline(value_text, line.lineno)
            idx += 1
        else:
            # Value is a block on subsequent lines.
            idx += 1
            if idx >= len(lines) or lines[idx].indent <= base_indent:
                # Empty block — treat as None to match YAML.
                out[key] = None
                continue
            child_indent = lines[idx].indent
            value, idx = _parse_block(lines, idx, child_indent)
            out[key] = value
    return out, idx


def _parse_block_list(
    lines: list[_Line], idx: int, base_indent: int
) -> tuple[list[object], int]:
    items: list[object] = []
    while idx < len(lines) and lines[idx].indent == base_indent and (
        lines[idx].content.startswith("- ") or lines[idx].content == "-"
    ):
        line = lines[idx]
        rest = line.content[2:] if line.content.startswith("- ") else ""
        rest = rest.strip()

        # Look ahead at the next non-empty line: if it's indented
        # more than the dash AND aligned with the rest's column,
        # we have a multi-line mapping item.
        if rest and ":" in rest and _looks_like_kv(rest):
            # The first key:value of a multi-line mapping list item.
            # Subsequent keys live at indent = base_indent + 2 (or
            # wherever the content after `- ` started).
            content_col = base_indent + 2
            first_key, first_value_text = _split_key_value(rest, line.lineno)
            if not _BARE_RX.match(first_key):
                die(
                    f"yaml-subset: line {line.lineno} key {first_key!r} "
                    f"is not a bare identifier"
                )
            item: dict[str, object] = {}
            if first_value_text:
                item[first_key] = _parse_inline(first_value_text, line.lineno)
                idx += 1
            else:
                idx += 1
                if idx < len(lines) and lines[idx].indent > content_col:
                    nested_indent = lines[idx].indent
                    value, idx = _parse_block(lines, idx, nested_indent)
                    item[first_key] = value
                else:
                    item[first_key] = None
            # Consume additional keys at content_col.
            while idx < len(lines) and lines[idx].indent == content_col:
                ln = lines[idx]
                if ln.content.startswith("- "):
                    break  # next list item, not a sibling key
                k, v_text = _split_key_value(ln.content, ln.lineno)
                if not _BARE_RX.match(k):
                    die(
                        f"yaml-subset: line {ln.lineno} key {k!r} is "
                        f"not a bare identifier"
                    )
                if k in item:
                    die(f"yaml-subset: line {ln.lineno} duplicate key {k!r}")
                if v_text:
                    item[k] = _parse_inline(v_text, ln.lineno)
                    idx += 1
                else:
                    idx += 1
                    if idx < len(lines) and lines[idx].indent > content_col:
                        nested_indent = lines[idx].indent
                        value, idx = _parse_block(lines, idx, nested_indent)
                        item[k] = value
                    else:
                        item[k] = None
            items.append(item)
        elif rest:
            # Inline scalar / inline list / inline dict on the dash line.
            items.append(_parse_inline(rest, line.lineno))
            idx += 1
        else:
            # Bare `-` — value is a block on subsequent lines.
            idx += 1
            if idx >= len(lines) or lines[idx].indent <= base_indent:
                items.append(None)
                continue
            child_indent = lines[idx].indent
            value, idx = _parse_block(lines, idx, child_indent)
            items.append(value)
    return items, idx


def _looks_like_kv(s: str) -> bool:
    """Heuristic: does `s` look like a mapping `key: value` line?
    True if there's an unquoted `:` that's followed by space-or-EOL."""
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            if i + 1 >= len(s) or s[i + 1] in (" ", "\t"):
                return True
    return False


# --- Public API -------------------------------------------------------------


def parse_yaml_subset(text: str) -> dict[str, object]:
    """Parse a YAML-subset document. Top level must be a mapping;
    otherwise we die with a clear pointer."""
    # Reject features that have no place in our schema before we
    # tokenize, with line numbers from the raw text.
    for n, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if s.startswith("|") or s.startswith(">") or s.startswith("- |") or s.startswith("- >"):
            die(
                f"yaml-subset: line {n} uses a multi-line block "
                f"scalar (`|` / `>`) — not supported. Use a quoted "
                f"single-line string instead."
            )
        if "&" in s or "*" in s:
            # Only flag when `&` or `*` is being used as anchor/alias,
            # not when it's inside a quoted string. Cheap check: any
            # bare `&foo:` / `*foo` at the start of a value position.
            if re.search(r"(^|\s)[&*][A-Za-z0-9_]+", s):
                die(
                    f"yaml-subset: line {n} uses anchors / aliases "
                    f"(`&` / `*`) — not supported."
                )
        if "!!" in s and not (s.count("'") % 2 or s.count('"') % 2):
            die(
                f"yaml-subset: line {n} uses a YAML tag (`!!`) — not "
                f"supported."
            )

    lines = _tokenize(text)
    if not lines:
        return {}
    base_indent = lines[0].indent
    if base_indent != 0:
        die(
            f"yaml-subset: top-level content must start in column 0 "
            f"(got column {base_indent} on line {lines[0].lineno})"
        )
    value, consumed = _parse_block(lines, 0, 0)
    if consumed < len(lines):
        die(
            f"yaml-subset: trailing content starting on line "
            f"{lines[consumed].lineno}"
        )
    if not isinstance(value, dict):
        die("yaml-subset: top-level value must be a mapping")
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Find `---` delimiters at the top of a Markdown file, parse
    the frontmatter as YAML subset, return (mapping, body_text).

    No frontmatter at all → ({}, text). Single opening `---` with
    no closing → die with a clear pointer. Body is the verbatim
    text after the closing `---` line (preserving original line
    endings)."""
    # Split into lines but preserve the original separators so the
    # body slice is exact.
    nl_positions: list[int] = []
    for i, ch in enumerate(text):
        if ch == "\n":
            nl_positions.append(i)
    if not nl_positions and not text:
        return {}, ""

    first_nl = nl_positions[0] if nl_positions else len(text)
    first_line = text[:first_nl].strip()
    if first_line != "---":
        return {}, text  # no frontmatter; whole document is body

    # Find the matching closing `---`.
    body_start = -1
    fm_end_lineno = -1
    line_starts = [0] + [p + 1 for p in nl_positions]
    for line_idx in range(1, len(line_starts)):
        ls = line_starts[line_idx]
        next_nl = nl_positions[line_idx] if line_idx < len(nl_positions) else len(text)
        line = text[ls:next_nl].rstrip()
        if line == "---":
            body_start = next_nl + 1 if next_nl < len(text) else next_nl
            fm_end_lineno = line_idx
            break
    if body_start < 0:
        die("frontmatter: opening `---` has no matching closing `---`")

    fm_text = text[line_starts[1]:line_starts[fm_end_lineno]] if fm_end_lineno > 1 else ""
    fm = parse_yaml_subset(fm_text)
    body = text[body_start:]
    return fm, body
