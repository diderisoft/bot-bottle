"""Keep a bottle file's frontmatter comments when bot-bottle rewrites it.

bot-bottle edits a bottle by parsing its frontmatter, changing the data and
serializing it again (`yaml_subset`), which drops comments. `keep_comments`
lines the old frontmatter up with the new one (comments aside) and puts
them back:

- a line that didn't change comes back exactly as it was written, with its
  inline comment and its formatting;
- a comment above a line comes back above that line, or, when that line
  changed or went, above what replaced it, so none is ever lost;
- an inline comment on a line that changed comes back on its own line above
  the replacement.

A `#` inside quotes or a block scalar (`|`, `>`) isn't a comment.
"""

from __future__ import annotations

import difflib
import re

_FENCE = "---"
_BLOCK_SCALAR = re.compile(r":\s*[|>][-+0-9]*\s*(#.*)?$")


def _split_inline(line: str) -> tuple[str, str]:
    """`line` as (content, its inline comment with the spaces before it)."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            content = line[:i].rstrip()
            return content, line[len(content):]
    return line.rstrip(), ""


def _classify(lines: list[str]) -> list[tuple[str, str]]:
    """Each line as (content, comment): a full-line comment or blank line has
    no content; a block scalar's lines are all content."""
    out: list[tuple[str, str]] = []
    scalar_indent = -1
    for line in lines:
        indent = len(line) - len(line.lstrip())
        if scalar_indent >= 0 and (not line.strip() or indent > scalar_indent):
            out.append((line, ""))
            continue
        scalar_indent = -1
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(("", line))
            continue
        content, comment = _split_inline(line)
        out.append((content, comment))
        if _BLOCK_SCALAR.search(content):
            scalar_indent = indent
    return out


def _frontmatter(text: str) -> tuple[list[str], str] | None:
    """The frontmatter's lines and the rest from its closing fence, or None
    when `text` has none."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            return lines[1:i], "\n".join(lines[i:])
    return None


def keep_comments(old: str, new: str) -> str:
    """`new` (a bottle file bot-bottle wrote from `old`'s data) with `old`'s
    frontmatter comments put back. `new` as it is when either has no
    frontmatter or `old` has no comments."""
    before, after = _frontmatter(old), _frontmatter(new)
    if before is None or after is None:
        return new
    old_lines, new_lines = before[0], after[0]
    classified = _classify(old_lines)
    if not any(comment for _, comment in classified):
        return new
    # The old content lines, each with the comment lines just above it.
    content: list[str] = []
    above: list[list[str]] = []
    verbatim: list[str] = []
    inline: list[str] = []
    pending: list[str] = []
    for line, (text, comment) in zip(old_lines, classified):
        if not text:
            pending.append(line)
            continue
        content.append(text)
        above.append(pending)
        verbatim.append(line)
        inline.append(comment)
        pending = []
    trailing = pending
    # The new text's own comment lines, if any, are the old ones again.
    kept = [(line, text) for line, (text, _) in zip(new_lines, _classify(new_lines)) if text]
    new_lines = [line for line, _ in kept]
    new_content = [text for _, text in kept]
    out: list[str] = []
    matcher = difflib.SequenceMatcher(None, content, new_content, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for i in range(i1, i2):
                out += above[i] + [verbatim[i]]
            continue
        if tag == "replace" and i2 - i1 == j2 - j1 and all(
                _same(content[i1 + k], new_content[j1 + k]) for k in range(i2 - i1)):
            # Written differently, meaning the same (quoting, say): as it was.
            for i in range(i1, i2):
                out += above[i] + [verbatim[i]]
            continue
        # Changed or gone: its comments go above what replaced it.
        for i in range(i1, i2):
            out += above[i]
            if inline[i]:
                indent = verbatim[i][:len(verbatim[i]) - len(verbatim[i].lstrip())]
                out.append(indent + inline[i].strip())
        out += new_lines[j1:j2]
    out += trailing
    # The body as it was written, when only the frontmatter changed.
    rest = before[1] if before[1].strip() == after[1].strip() else after[1]
    return "\n".join([_FENCE, *out, rest])


def _same(old: str, new: str) -> bool:
    """Whether two frontmatter lines at the same indent say the same thing."""
    # pylint: disable-next=import-outside-toplevel
    from .yaml_subset import YamlSubsetError, parse_yaml_subset
    indent = len(old) - len(old.lstrip())
    if indent != len(new) - len(new.lstrip()):
        return False
    try:
        return parse_yaml_subset(old.strip()) == parse_yaml_subset(new.strip())
    except YamlSubsetError:
        return False


__all__ = ["keep_comments"]
