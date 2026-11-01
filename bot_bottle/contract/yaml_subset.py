"""A stdlib-only YAML-subset parser for manifests, with no implicit coercion
(no Norway problem, no surprise dates or octals).

    parse_yaml_subset(text) -> dict[str, object]
    parse_frontmatter(text) -> (frontmatter dict, body text)
    serialize_yaml_subset(data) -> str   (round-trips through the parser)

Accepted, block style:

    key: value
    key:
      nested_key: value
    key:
      - item
      - subkey: value1
        subkey2: value2

and inline with scalar leaves: `key: [a, b, "c d"]`, `key: {a: 1, b: 2}`.

Accepted block scalars:

    key: |
      line one
      line two

`|` (literal, clip chomp) is supported; `|-` (strip) and `|+` (keep) too.
`>` (folded) is not supported.

Rejected with a line number: anchors and aliases, tags, `>`/`>-`/`>+` folded
scalars, standalone `|` as a value, yes/no/on/off, ambiguous bare scalars
(numbers, dates), tab indentation, and flow nesting deeper than one level.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast


class YamlSubsetError(ValueError):
    """Input breaks the subset's rules. CLI callers turn it into `die`; the
    egress addon handles it as an exception."""



# --- Tokenizer ---


@dataclass(frozen=True)
class _Line:
    """One non-blank, non-comment line: `indent` is the first non-space column,
    `content` the text from there with trailing whitespace and `# ...` comments
    stripped, `lineno` 1-based."""

    indent: int
    content: str
    lineno: int


def _strip_trailing_comment(s: str) -> str:
    """Strip a trailing ` # comment` unless the `#` is inside quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # `#` starts a comment only after whitespace.
            if i == 0 or s[i - 1] in (" ", "\t"):
                return s[:i].rstrip()
    return s.rstrip()


def _tokenize(text: str) -> list[_Line]:
    """Drop blank and comment lines and split the rest into indent and content.
    Tabs in the indent are rejected."""
    out: list[_Line] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        leading = len(raw) - len(raw.lstrip(" \t"))
        if "\t" in raw[:leading]:
            raise YamlSubsetError(f"yaml-subset: tab character in indent on line {n}")
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = _strip_trailing_comment(raw[indent:])
        if not content:
            continue
        out.append(_Line(indent=indent, content=content, lineno=n))
    return out


# --- Scalar parsing ---


_BARE_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def _key(raw: str) -> str | None:
    """A mapping key: a bare identifier, or a quoted string (for keys like
    `codex@gitea.example.com`) without escapes; None if it's neither."""
    if _BARE_RX.match(raw):
        return raw
    if len(raw) >= 3 and raw[0] == raw[-1] and raw[0] in "'\"":
        inner = raw[1:-1]
        if raw[0] not in inner and "\\" not in inner:
            return inner
    return None
_INT_RX = re.compile(r"^-?[0-9]+$")
_RESERVED_BOOL_LIKE = frozenset({"yes", "no", "on", "off", "y", "n", "Y", "N",
                                 "YES", "NO", "ON", "OFF", "True", "False",
                                 "TRUE", "FALSE"})
# Unquoted tokens that look like other literals (dates, octals, ...) are
# rejected with a "quote it" hint, or parsed as true/false/null/int.
_DATE_RX = re.compile(r"^-?\d{4}-\d{2}-\d{2}(T\d.*)?$")
_OCTAL_RX = re.compile(r"^0o?\d+$")
_HEX_RX = re.compile(r"^0x[0-9A-Fa-f]+$")
_FLOAT_RX = re.compile(r"^-?\d+\.\d+([eE][-+]?\d+)?$")


def _parse_scalar(s: str, lineno: int) -> object:
    """A stripped value as str, int, bool or None. Quoted strings keep their
    content (with escapes); bare strings only when unambiguous."""
    s = s.strip()
    if not s:
        return ""

    # Quoted forms first.
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        if len(s) < 2:
            raise YamlSubsetError(f"yaml-subset: unterminated quoted string on line {lineno}")
        body = s[1:-1]
        if s.startswith('"'):
            # JSON-style escapes for double quotes.
            try:
                return body.encode("utf-8").decode("unicode_escape")
            except UnicodeDecodeError as e:
                raise YamlSubsetError(f"yaml-subset: bad escape on line {lineno}: {e}")
        else:
            # Single quotes: only '' -> '.
            return body.replace("''", "'")

    # Bool-like tokens other than true/false are rejected.
    if s in _RESERVED_BOOL_LIKE:
        if s in ("true", "false"):
            return s == "true"
        raise YamlSubsetError(
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

    # Look-alikes we reject.
    if _DATE_RX.match(s):
        raise YamlSubsetError(
            f"yaml-subset: bare {s!r} on line {lineno} looks like a "
            f"date — quote it as a string or use an explicit int"
        )
    if _OCTAL_RX.match(s):
        raise YamlSubsetError(
            f"yaml-subset: bare {s!r} on line {lineno} looks like an "
            f"octal/0-prefixed integer — quote it as a string"
        )
    if _HEX_RX.match(s):
        raise YamlSubsetError(
            f"yaml-subset: bare {s!r} on line {lineno} looks like a "
            f"hex integer — quote it as a string"
        )
    if _FLOAT_RX.match(s):
        raise YamlSubsetError(
            f"yaml-subset: floats not supported (line {lineno}, "
            f"value {s!r}); use an int or quote as a string"
        )

    # Whatever's left is unambiguously a string (URLs, paths, hostnames).
    return s


# --- Inline list / dict ---


def _parse_inline(s: str, lineno: int) -> object:
    """Inline `[a, b]`, `{a: 1, b: 2}` or a scalar. Deeper flow nesting is
    rejected."""
    s = s.strip()
    if s.startswith("["):
        if not s.endswith("]"):
            raise YamlSubsetError(f"yaml-subset: unterminated `[` on line {lineno}")
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
            raise YamlSubsetError(f"yaml-subset: unterminated `{{` on line {lineno}")
        body = s[1:-1].strip()
        if not body:
            return {}
        out: dict[str, object] = {}
        for raw in _split_flow(body, lineno, "dict"):
            if ":" not in raw:
                raise YamlSubsetError(
                    f"yaml-subset: inline dict entry on line {lineno} "
                    f"missing `:` ({raw!r})"
                )
            k, _, v = raw.partition(":")
            k = k.strip()
            k_key = _key(k)
            if k_key is None:
                raise YamlSubsetError(
                    f"yaml-subset: inline dict key on line {lineno} "
                    f"must be a bare identifier or quoted ({k!r})"
                )
            k = k_key
            out[k] = _parse_scalar(v.strip(), lineno)
        return out
    return _parse_scalar(s, lineno)


def _split_flow(body: str, lineno: int, kind: str) -> list[str]:
    """Split `a, b, c`, respecting quotes. Nested flow is rejected."""
    items: list[str] = []
    depth_b = 0
    depth_c = 0
    in_single = False
    in_double = False
    cur: list[str] = []
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
                raise YamlSubsetError(
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


# --- Block parser ---


def _split_key_value(content: str, lineno: int) -> tuple[str, str]:
    """Split at the first unquoted `:` separating key and value; `value` may be
    empty (a block mapping)."""
    in_single = False
    in_double = False
    for i, ch in enumerate(content):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ":" and not in_single and not in_double:
            # `:` separates only before a space or at end of line (not in
            # `key:value` URLs).
            if i + 1 >= len(content) or content[i + 1] in (" ", "\t"):
                return content[:i].strip(), content[i + 1:].lstrip()
    raise YamlSubsetError(f"yaml-subset: line {lineno} missing `: ` separator: {content!r}")


def _parse_block(
    lines: list[_Line], idx: int, base_indent: int
) -> tuple[object, int]:
    """Parse the block at `lines[idx]` at `base_indent`. Returns (value, index
    of the first unconsumed line)."""
    if idx >= len(lines):
        raise YamlSubsetError("yaml-subset: unexpected end of document")
    first = lines[idx]
    if first.indent < base_indent:
        raise YamlSubsetError(
            f"yaml-subset: line {first.lineno} indented less than "
            f"expected (got {first.indent}, expected >= {base_indent})"
        )
    if first.indent > base_indent:
        raise YamlSubsetError(
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
            raise YamlSubsetError(
                f"yaml-subset: line {line.lineno} unexpected list "
                f"item at mapping indent (got `-`, expected `key:`)"
            )
        key, value_text = _split_key_value(line.content, line.lineno)
        key_key = _key(key)
        if key_key is None:
            raise YamlSubsetError(
                f"yaml-subset: line {line.lineno} key {key!r} is not "
                f"a bare identifier or quoted"
            )
        key = key_key
        if key in out:
            raise YamlSubsetError(
                f"yaml-subset: line {line.lineno} duplicate key {key!r}"
            )
        if value_text:
            out[key] = _parse_inline(value_text, line.lineno)
            idx += 1
        else:
            # The value is a block on the following lines.
            idx += 1
            if idx >= len(lines) or lines[idx].indent <= base_indent:
                # An empty block is None, as in YAML.
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

        # A multi-line mapping item continues on lines aligned with its first
        # key.
        if rest and ":" in rest and _looks_like_kv(rest):
            # Further keys sit at the column after `- `.
            content_col = base_indent + 2
            first_key, first_value_text = _split_key_value(rest, line.lineno)
            first_key_key = _key(first_key)
            if first_key_key is None:
                raise YamlSubsetError(
                    f"yaml-subset: line {line.lineno} key {first_key!r} "
                    f"is not a bare identifier or quoted"
                )
            first_key = first_key_key
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
            # Consume the item's further keys.
            while idx < len(lines) and lines[idx].indent == content_col:
                ln = lines[idx]
                if ln.content.startswith("- "):
                    break  # next list item
                k, v_text = _split_key_value(ln.content, ln.lineno)
                k_key = _key(k)
                if k_key is None:
                    raise YamlSubsetError(
                        f"yaml-subset: line {ln.lineno} key {k!r} is "
                        f"not a bare identifier or quoted"
                    )
                k = k_key
                if k in item:
                    raise YamlSubsetError(f"yaml-subset: line {ln.lineno} duplicate key {k!r}")
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
            # Inline scalar, list or dict on the dash line.
            items.append(_parse_inline(rest, line.lineno))
            idx += 1
        else:
            # Bare `-`: the value is a block below.
            idx += 1
            if idx >= len(lines) or lines[idx].indent <= base_indent:
                items.append(None)
                continue
            child_indent = lines[idx].indent
            value, idx = _parse_block(lines, idx, child_indent)
            items.append(value)
    return items, idx


def _looks_like_kv(s: str) -> bool:
    """Whether `s` looks like `key: value` (an unquoted `:` before a space or
    end)."""
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


# --- Block scalar pre-processor ---

# Matches `key: |`, `key: |-`, `key: |+` (optionally with trailing comment).
# Requires at least one space between `:` and `|` so `key:|` is not treated
# as a block scalar (yaml-subset would reject it as a missing `: ` anyway).
_BLOCK_SCALAR_LINE_RE = re.compile(
    r"^(\s*)(\S.*?):\s+(\|[-+]?)\s*(?:#.*)?$"
)


def _preprocess_block_scalars(text: str) -> str:
    """Convert `key: |` literal block scalar values to JSON-encoded double-quoted
    strings before tokenizing. The tokenizer drops blank lines, so block scalar
    bodies — where blank lines are content — must be extracted from the raw text.

    Supports `|` (clip: one trailing newline), `|-` (strip: no trailing newline),
    and `|+` (keep: all trailing newlines preserved). `>` (folded) is not
    supported and remains as-is; the pre-scan will reject it."""
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("#"):
            result.append(line)
            i += 1
            continue
        m = _BLOCK_SCALAR_LINE_RE.match(line)
        if m:
            key_indent = len(m.group(1))
            chomp = m.group(3)
            prefix = line[:m.start(3)].rstrip()  # 'indent + key:' without trailing space
            i += 1
            block_indent: int | None = None
            content_parts: list[str] = []
            trailing_blank_count = 0
            while i < len(lines):
                curr = lines[i]
                if not curr.strip():
                    if block_indent is not None:
                        trailing_blank_count += 1
                    i += 1
                    continue
                curr_indent = len(curr) - len(curr.lstrip())
                if curr_indent <= key_indent:
                    break
                if block_indent is None:
                    block_indent = curr_indent
                content_parts.extend([""] * trailing_blank_count)
                trailing_blank_count = 0
                content_parts.append(
                    curr[block_indent:] if curr_indent >= block_indent else curr.lstrip()
                )
                i += 1
            # `text.split("\n")` always produces a terminal empty string from the
            # final `\n`. When the block reaches the end of the text, that artifact
            # inflates `trailing_blank_count` by one; compensate for it.
            at_end = i >= len(lines)
            real_trailing = max(0, trailing_blank_count - (1 if at_end else 0))
            content = "\n".join(content_parts)
            if chomp == "|":
                content += "\n"
            elif chomp == "|+":
                content += "\n" * (1 + real_trailing)
            # |-: strip — no trailing newline added
            result.append(f"{prefix} {json.dumps(content)}")
        else:
            result.append(line)
            i += 1
    return "\n".join(result)


# --- Public API ---


def parse_yaml_subset(text: str) -> dict[str, object]:
    """Parse a YAML-subset document; the top level must be a mapping."""
    text = _preprocess_block_scalars(text)
    # Reject unsupported features, with raw line numbers.
    for n, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        # `key: |` block scalars were already converted by _preprocess_block_scalars.
        # A bare `|` here (after pre-processing) is an invalid standalone value.
        if s.startswith(">") or s.startswith("- >"):
            raise YamlSubsetError(
                f"yaml-subset: line {n} uses a folded block scalar (`>`) "
                f"— not supported. Use `|` for a literal block scalar or a "
                f"quoted single-line string instead."
            )
        if s.startswith("|") or s.startswith("- |"):
            raise YamlSubsetError(
                f"yaml-subset: line {n} has a bare `|` value that could not "
                f"be pre-processed as a block scalar. Write `key: |` with the "
                f"block content indented on the following lines."
            )
        if "&" in s or "*" in s:
            # Flag `&`/`*` only as an anchor or alias at a value position.
            if re.search(r"(^|\s)[&*][A-Za-z0-9_]+", s):
                raise YamlSubsetError(
                    f"yaml-subset: line {n} uses anchors / aliases "
                    f"(`&` / `*`) — not supported."
                )
        if "!!" in s and not (s.count("'") % 2 or s.count('"') % 2):
            raise YamlSubsetError(
                f"yaml-subset: line {n} uses a YAML tag (`!!`) — not "
                f"supported."
            )

    lines = _tokenize(text)
    if not lines:
        return {}
    base_indent = lines[0].indent
    if base_indent != 0:
        raise YamlSubsetError(
            f"yaml-subset: top-level content must start in column 0 "
            f"(got column {base_indent} on line {lines[0].lineno})"
        )
    value, consumed = _parse_block(lines, 0, 0)
    if consumed < len(lines):
        raise YamlSubsetError(
            f"yaml-subset: trailing content starting on line "
            f"{lines[consumed].lineno}"
        )
    if not isinstance(value, dict):
        raise YamlSubsetError("yaml-subset: top-level value must be a mapping")
    return cast(dict[str, object], value)


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Parse `---`-delimited frontmatter at the top of a Markdown file. Returns
    (mapping, body). No frontmatter gives ({}, text); an unclosed one dies. The
    body is the exact text after the closing `---`."""
    # Keep the original separators so the body slice is exact.
    nl_positions: list[int] = []
    for i, ch in enumerate(text):
        if ch == "\n":
            nl_positions.append(i)
    if not nl_positions and not text:
        return {}, ""

    first_nl = nl_positions[0] if nl_positions else len(text)
    first_line = text[:first_nl].strip()
    if first_line != "---":
        return {}, text  # no frontmatter

    # Find the closing `---`.
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
        raise YamlSubsetError("frontmatter: opening `---` has no matching closing `---`")

    fm_text = text[line_starts[1]:line_starts[fm_end_lineno]] if fm_end_lineno > 1 else ""
    fm = parse_yaml_subset(fm_text)
    body = text[body_start:]
    return fm, body


# --- Serializer ---


def _needs_quoting(s: str) -> bool:
    """True when the string must be single-quoted to round-trip."""
    if not s:
        return True
    if s in ("true", "false", "null", "~") or s in _RESERVED_BOOL_LIKE:
        return True
    if (
        _INT_RX.match(s)
        or _DATE_RX.match(s)
        or _OCTAL_RX.match(s)
        or _HEX_RX.match(s)
        or _FLOAT_RX.match(s)
    ):
        return True
    # Characters with special meaning at the start of a value.
    if s[0] in ('"', "'", "[", "{", "!", "&", "*", "#", "|", ">", "%", "@", "`"):
        return True
    return False


def _yaml_scalar(v: object) -> str:
    """A scalar as YAML text."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if _needs_quoting(s):
        return "'" + s.replace("'", "''") + "'"
    return s


def _yaml_key(key: object) -> str:
    """`key` as written: bare when it can be, else double-quoted."""
    text = str(key)
    return text if _BARE_RX.match(text) else json.dumps(text)


def _serialize_node(node: object, indent: int) -> list[str]:
    """Lines for `node` at `indent`: a non-empty dict or list, or a leaf
    scalar."""
    prefix = " " * indent
    if isinstance(node, dict):
        out: list[str] = []
        for key, val in node.items():
            if isinstance(val, (dict, list)) and val:
                out.append(f"{prefix}{_yaml_key(key)}:")
                out.extend(_serialize_node(val, indent + 2))
            else:
                scalar = (
                    "{}" if isinstance(val, dict)
                    else "[]" if isinstance(val, list)
                    else _yaml_scalar(val)
                )
                out.append(f"{prefix}{_yaml_key(key)}: {scalar}")
        return out
    if isinstance(node, list):
        out = []
        for item in node:
            if isinstance(item, dict) and item:
                entries = list(item.items())
                first_key, first_val = entries[0]
                if isinstance(first_val, (dict, list)) and first_val:
                    out.append(f"{prefix}- {_yaml_key(first_key)}:")
                    out.extend(_serialize_node(first_val, indent + 4))
                else:
                    scalar = (
                        "{}" if isinstance(first_val, dict)
                        else "[]" if isinstance(first_val, list)
                        else _yaml_scalar(first_val)
                    )
                    out.append(f"{prefix}- {_yaml_key(first_key)}: {scalar}")
                cont = prefix + "  "
                for key, val in entries[1:]:
                    if isinstance(val, (dict, list)) and val:
                        out.append(f"{cont}{_yaml_key(key)}:")
                        out.extend(_serialize_node(val, indent + 4))
                    else:
                        scalar = (
                            "{}" if isinstance(val, dict)
                            else "[]" if isinstance(val, list)
                            else _yaml_scalar(val)
                        )
                        out.append(f"{cont}{_yaml_key(key)}: {scalar}")
            else:
                out.append(f"{prefix}- {_yaml_scalar(item)}")
        return out
    return [_yaml_scalar(node)]  # pragma: no cover


def serialize_yaml_subset(data: dict[str, object]) -> str:
    """Serialize `data` as block-style YAML with 2-space indent and a trailing
    newline, in key insertion order. Round-trips through `parse_yaml_subset`."""
    if not data:
        return ""
    return "\n".join(_serialize_node(data, 0)) + "\n"
