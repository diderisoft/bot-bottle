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


_LOG_NAMES = {0: "off", 1: "blocked", 2: "full"}


def _egress(doc: Doc, changes: list[str]) -> None:
    egress = doc.get("egress")
    if not isinstance(egress, dict):
        return
    log = egress.get("log")
    if isinstance(log, int) and not isinstance(log, bool) and log in _LOG_NAMES:
        egress["log"] = _LOG_NAMES[log]
        changes.append(f"egress.log {log} is {_LOG_NAMES[log]}")
    routes = egress.get("routes")
    if not isinstance(routes, list):
        return
    for i, route in enumerate(routes):
        if not isinstance(route, dict) or route.get("blocked") is True:
            continue
        new = new_route(route)
        if new != route:
            routes[i] = new
            changes.append(f"egress route {route.get('host')}: in the current shape")


def new_route(route: Mapping[str, Any]) -> Doc:
    """A route in the current shape. `inspect` goes: `inspect: false` is
    `passthrough: true`; its `auth` (in the current form), `git` and
    `preserve_auth` sit on the route; `matches` is `allow`, each entry
    without methods getting `methods: ["*"]` (it allowed any); the detectors
    are `dlp`. A route with no matches but injected auth or preserve_auth
    allowed any method, so it gets `methods: ["*"]`. `role` is dropped."""
    host = str(route.get("host") or "?")
    if not any(k in route for k in ("inspect", "role", "matches")):
        allow = route.get("allow")
        if not isinstance(allow, list):
            return dict(route)
        return {**route, "allow": [_new_entry(e, keep_methods=True) for e in allow]}
    rest = {k: v for k, v in route.items() if k not in ("host", "inspect", "role", "matches")}
    out: Doc = {"host": route.get("host")}
    inspect = route.get("inspect", {})
    if inspect is False:
        out["passthrough"] = True
        return {**out, **rest}
    inspect = dict(inspect) if isinstance(inspect, dict) else {}
    if "matches" in route and "matches" not in inspect:
        inspect["matches"] = route["matches"]
    for key in ("auth", "git", "preserve_auth", "dlp", "methods", "allow"):
        if key in rest and key not in inspect:
            inspect[key] = rest.pop(key)
    auth = inspect.get("auth")
    if isinstance(auth, dict) and any(k in auth for k in _OLD_AUTH_KEYS):
        auth = new_auth(host, auth)
    allow = inspect.get("allow")
    matches = inspect.get("matches")
    if isinstance(matches, list):
        allow = [_new_entry(e) for e in matches]
    elif isinstance(allow, list):
        allow = [_new_entry(e, keep_methods=True) for e in allow]
    methods = inspect.get("methods")
    if isinstance(matches, list) and isinstance(allow, list) and len(allow) == 1 \
            and set(allow[0]) <= {"methods"} and methods is None:
        # One entry of methods alone is the route's methods; GET and HEAD
        # alone are the default, said by saying nothing.
        methods = allow[0].get("methods")
        allow = None
        if isinstance(methods, list) and sorted(str(m).upper() for m in methods) == \
                ["GET", "HEAD"]:
            methods = None
    if methods is None and allow is None and matches is None and (
            auth is not None or inspect.get("preserve_auth") is True):
        methods = ["*"]
    if methods is not None:
        out["methods"] = methods
    if allow is not None:
        out["allow"] = allow
    if auth is not None:
        out["auth"] = auth
    for key in ("git", "preserve_auth"):
        if key in inspect:
            out[key] = inspect[key]
    dlp = _new_dlp(inspect)
    if dlp is not None:
        out["dlp"] = dlp
    return {**out, **{k: v for k, v in rest.items() if k not in out}}


def _new_entry(entry: Any, *, keep_methods: bool = False) -> Any:
    """A `matches` entry as an `allow` one: its paths in the current form,
    and `methods: ["*"]` when it had none (it allowed any)."""
    if not isinstance(entry, dict):
        return entry
    out = dict(entry)
    if isinstance(out.get("paths"), list):
        out["paths"] = [_new_path(p) for p in out["paths"]]
    if "methods" not in out and not keep_methods:
        out["methods"] = ["*"]
    return out


def _new_path(path: Any) -> Any:
    """`{type: prefix, value: X}` (or no type) is `X`; exact and regex are
    `{exact: X}` and `{regex: X}`."""
    if not isinstance(path, dict) or "value" not in path:
        return path
    kind = path.get("type", "prefix")
    value = path["value"]
    return value if kind == "prefix" else {kind: value}


def _new_dlp(inspect: Mapping[str, Any]) -> Any:
    """`outbound_detectors`, `inbound_detectors`, `outbound_on_match` as
    `dlp` (both detector lists false is `dlp: false`); None when unset."""
    if "dlp" in inspect:
        return inspect["dlp"]
    out_d, in_d = inspect.get("outbound_detectors"), inspect.get("inbound_detectors")
    on_match = inspect.get("outbound_on_match")
    if out_d is False and in_d is False and not on_match:
        return False
    dlp: Doc = {}
    if out_d is not None:
        dlp["outbound"] = [] if out_d is False else list(out_d)
    if in_d is not None:
        dlp["inbound"] = [] if in_d is False else list(in_d)
    if on_match:
        dlp["on_match"] = on_match
    return dlp or None


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
