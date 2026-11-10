"""What `bottles migrate` / `agents migrate` turn an old manifest into, as
plain data, and how the file's text is rewritten to say it.

`upgrade_bottle` and `upgrade_agent` take a file's frontmatter and return it
in the current format, with a line per change. `rewrite_text` writes that
into the file's text, keeping what it can: each top-level block whose value
is unchanged stays as written (comments included; a renamed key only has its
key changed), a changed block is written afresh, and the body is untouched.
Small edits (a `?` gaining `secret:`) are made on the line itself first, so
their block keeps its comments.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter, serialize_yaml_subset
from ..secret_ref import is_name, stored
from .util import ManifestError


class UpgradeError(ManifestError):
    """A part of the file has no new form; the message says what to write."""


Doc = dict[str, Any]
# A textual edit: the frontmatter's lines in, lines out.
LineEdit = Callable[[list[str]], list[str]]


# --- Bottles -----------------------------------------------------------------

def upgrade_bottle(fm: Mapping[str, Any]) -> tuple[Doc, list[str], list[LineEdit]]:
    """`fm` (a bottle's frontmatter) in the current format, what changed, and
    the line edits that say part of it in place."""
    doc: Doc = copy.deepcopy(dict(fm))
    changes: list[str] = []
    edits: list[LineEdit] = []
    _env_prompts(doc, changes, edits)
    _egress(doc, changes)
    return doc, changes, edits


def _env_prompts(doc: Doc, changes: list[str], edits: list[LineEdit]) -> None:
    """A bare `?`/`?msg` was a secret; it is now `secret:?`/`secret:?msg`."""
    env = doc.get("env")
    if not isinstance(env, dict):
        return
    names = [k for k, v in env.items() if isinstance(v, str) and v.startswith("?")]
    for name in names:
        env[name] = "secret:" + env[name]
    if names:
        changes.append(f"env {', '.join(names)}: ? is now secret:? (asked, hidden)")
        edits.append(lambda lines: _in_block(lines, "env", _secret_prompt_line))


_PROMPT_LINE = re.compile(r"^(?P<pre>\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*)(?P<q>[\"']?)\?")


def _secret_prompt_line(line: str) -> str:
    return _PROMPT_LINE.sub(lambda m: f"{m['pre']}{m['q']}secret:?", line, count=1)


# --- Egress ------------------------------------------------------------------

_OLD_AUTH_KEYS = ("scheme", "header", "token_ref")


def _egress(doc: Doc, changes: list[str]) -> None:
    egress = doc.get("egress")
    routes = egress.get("routes") if isinstance(egress, dict) else None
    if not isinstance(routes, list):
        return
    for route in routes:
        if not isinstance(route, dict):
            continue
        host = str(route.get("host") or "?")
        inspect = route.get("inspect")
        auth = inspect.get("auth") if isinstance(inspect, dict) else None
        if isinstance(inspect, dict) and isinstance(auth, dict) and \
                any(k in auth for k in _OLD_AUTH_KEYS):
            inspect["auth"] = new_auth(host, auth)
            changes.append(f"egress route {host}: auth is type and value")


def new_auth(host: str, auth: Mapping[str, Any]) -> Doc:
    """An old-format auth as `type`, `name` (a header's) and `value`:
    `scheme: Bearer` is `type: bearer`, `scheme: token` is `type: token`,
    `header: X` alone is `type: header, name: X`, `token_ref: N` is `value:
    secret:N`. Raises UpgradeError for what has no new form, saying what to
    write by hand."""
    where = f"egress route {host}"
    scheme, header = auth.get("scheme"), auth.get("header")
    if scheme and header:
        raise UpgradeError(
            f"{where}: auth sends {header}: {scheme} <value> (header and scheme together), "
            f"which auth no longer does. Write it by hand: for Authorization, type: "
            f"{str(scheme).lower()} with the value; else type: header, name: {header}, "
            f"with the secret's value starting '{scheme} '")
    if any(k in auth for k in ("type", "name")):
        raise UpgradeError(f"{where}: auth mixes the old keys with type or name; edit it "
                           f"by hand")
    out: Doc = {}
    if scheme:
        if scheme not in ("Bearer", "token"):
            raise UpgradeError(f"{where}: auth.scheme {scheme!r} isn't Bearer or token; "
                               f"edit it by hand")
        out["type"] = str(scheme).lower()
    elif header:
        out["type"] = "header"
        out["name"] = header
    else:
        raise UpgradeError(f"{where}: auth has no scheme or header; edit it by hand")
    ref = auth.get("token_ref")
    if ref is not None:
        if "value" in auth:
            raise UpgradeError(f"{where}: auth sets both token_ref and value; edit it by hand")
        if not isinstance(ref, str) or not is_name(ref):
            raise UpgradeError(f"{where}: auth.token_ref {ref!r} isn't a secret name "
                               f"(letters, digits and _); edit it by hand")
        out["value"] = stored(ref)
    elif "value" in auth:
        out["value"] = auth["value"]
    out.update({k: v for k, v in auth.items() if k not in (*_OLD_AUTH_KEYS, "value")})
    return out


# --- Agents ------------------------------------------------------------------

def upgrade_agent(fm: Mapping[str, Any]) -> tuple[Doc, list[str], list[LineEdit]]:
    """`fm` (an agent's frontmatter) in the current format, what changed, and
    the line edits that say part of it in place."""
    doc: Doc = copy.deepcopy(dict(fm))
    return doc, [], []


# --- Text --------------------------------------------------------------------

_KEY_RE = re.compile(r"^(?P<key>\"[^\"]*\"|'[^']*'|[^\s:#'\"][^:#]*?)\s*:(?:\s|$)")


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _top_key(line: str) -> str | None:
    if not _content(line) or line[:1].isspace():
        return None
    m = _KEY_RE.match(line)
    if not m:
        return None
    key = m["key"]
    return key[1:-1] if key[:1] in "\"'" else key


def _blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """Each top-level key's block as (key, start, end): its line and the
    indented lines under it, not its trailing blank or comment lines."""
    starts = [(i, k) for i, line in enumerate(lines) if (k := _top_key(line)) is not None]
    out: list[tuple[str, int, int]] = []
    for n, (i, key) in enumerate(starts):
        stop = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        end = i + 1
        for j in range(i + 1, stop):
            if _content(lines[j]):
                end = j + 1
        out.append((key, i, end))
    return out


def _in_block(lines: list[str], key: str, edit: Callable[[str], str]) -> list[str]:
    """`lines` with `edit` applied to each line under top-level `key`."""
    out = list(lines)
    for k, start, end in _blocks(lines):
        if k == key:
            for i in range(start + 1, end):
                out[i] = edit(out[i])
    return out


def rename_top_key(old: str, new: str) -> LineEdit:
    """A line edit renaming top-level key `old` to `new`."""
    def edit(lines: list[str]) -> list[str]:
        out = list(lines)
        for k, start, _ in _blocks(lines):
            if k == old:
                out[start] = re.sub(rf"^[\"']?{re.escape(old)}[\"']?", new, out[start], count=1)
        return out
    return edit


def _serialized(key: str, value: Any) -> list[str]:
    return serialize_yaml_subset({key: value}).rstrip("\n").split("\n")


def rewrite_text(text: str, doc: Mapping[str, Any], edits: list[LineEdit]) -> str:
    """`text` (a manifest file) with its frontmatter saying `doc`: `edits`
    first, then each top-level block that still differs written afresh, a key
    `doc` drops removed, and a new one added at the end. Raises UpgradeError
    when the result wouldn't read back as `doc`."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise UpgradeError("the file doesn't start with --- frontmatter")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
    if close < 0:
        raise UpgradeError("the frontmatter has no closing ---")
    fm_lines = lines[1:close]
    for edit in edits:
        fm_lines = edit(fm_lines)
    try:
        now = parse_frontmatter("\n".join(["---", *fm_lines, "---", ""]))[0]
    except YamlSubsetError:
        now = {}
    out: list[str] = []
    last = 0
    seen: set[str] = set()
    for key, start, end in _blocks(fm_lines):
        out += fm_lines[last:start]
        last = end
        seen.add(key)
        if key not in doc:
            continue
        if key in now and now[key] == doc[key]:
            out += fm_lines[start:end]
        else:
            out += _serialized(key, doc[key])
    out += fm_lines[last:]
    for key, value in doc.items():
        if key not in seen:
            out += _serialized(key, value)
    result = "\n".join([lines[0], *out, *lines[close:]])
    try:
        after = parse_frontmatter(result)[0]
    except YamlSubsetError as e:
        raise UpgradeError(f"couldn't rewrite it safely ({e}); edit it by hand") from e
    if dict(after) != dict(doc):
        raise UpgradeError("couldn't rewrite it safely (its layout is unusual); "
                           "edit it by hand")
    return result


__all__ = ["UpgradeError", "rename_top_key", "rewrite_text", "upgrade_agent", "upgrade_bottle"]
