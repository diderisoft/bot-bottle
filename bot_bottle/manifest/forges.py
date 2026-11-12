"""Forges and one-line repo grants (PRD 0103, ADR 0019).

A bottle describes a forge under `git_gate.forges`, keyed by its host, and
grants repos on it in a line:

  git_gate:
    forges:
      gitea.example.com:                 # required: what to connect elsewhere
        kind: gitea
        url: https://gitea.example.com
        ssh: ssh://git@gitea.example.com:2222
    repos:
      - gitea.example.com/acme/app:write
      - gitea.example.com/acme/lib

A grant's prefix is the forge's real host. The host's connection to it
(`forge_connections`, written by `bot-bottle forge connect`) supplies the URL,
SSH address, pinned host key and token a launch uses; a bottle's description
never overrides them. It's the hint for connecting the forge on a machine
that hasn't, and its `kind` (and `host_key`, if pinned) must match the
connection's. A bottle never carries a token.

Each grant expands, while `extends` is resolved, into today's long-form repo
entry: a deploy key minted per launch through the forge, read-only unless
`:write`. Everything downstream sees only the long form. A bottle file can't
write that long form itself for a forge-minted key (ADR 0008). The long form
stays for a `static` key, an operator's own key file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from ..contract.forges import FORGE_KINDS
from .legacy_git_gate import OldGitGateFormatError, old_repos
from .util import ManifestError, as_json_object

_FORGE_KEYS = {"kind", "url", "ssh", "host_key"}
# Connection settings that live on the host now (ADR 0019), not in a bottle.
_HOST_KEYS = {"token", "account", "app", "transport"}
# A forge's key: its host, with a port if it has one.
_FORGE_NAME_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_CONNECT = "bot-bottle forge connect"
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ManifestForge:
    """One forge as a launch uses it: the host's connection (`name` is its
    host) and the user whose token it pushes with (`account`; its token is the
    host secret `token_secret`)."""

    name: str
    kind: str
    url: str
    ssh: str
    token_secret: str
    account: str = ""
    host_key: str = ""
    # A GitHub forge reached through the bot-bottle GitHub App's user token
    # (ADR 0012): the app's slug, for its install link.
    app: str = ""
    # GitHub only: "deploy-key" opts its repos into a key minted per launch
    # instead of HTTPS with the token (ADR 0014). Empty is the default.
    transport: str = ""

    def key(self, writable: bool) -> dict[str, object]:
        """The long-form `key` block a grant on this forge expands to."""
        key: dict[str, object] = {"provider": self.kind, "forge_token_env": self.token_secret,
                                  "api_url": self.url, "read_only": not writable}
        if self.transport:
            key["transport"] = self.transport
        return key


def _str(d: dict[str, object], key: str, label: str, *, required: bool = False) -> str:
    value = d.get(key, "")
    if not isinstance(value, str):
        raise ManifestError(f"{label}.{key} must be a string")
    if required and not value:
        raise ManifestError(f"{label} is missing {key!r}")
    return value


def credentials_raw(bottle_name: str, raw: dict[str, object]) -> dict[str, object]:
    """A bottle's `credentials:`, which only carried forge tokens: those are
    the host's now (ADR 0019), so the block is refused."""
    if "credentials" in raw:
        raise ManifestError(
            f"bottle '{bottle_name}' credentials: a forge's token is the host's "
            f"connection now, not the bottle's; connect the forge with `{_CONNECT} <url>` "
            f"and remove this block and the forge's token")
    return {}


def forges_raw(bottle_name: str, raw: dict[str, object]) -> dict[str, object]:
    """The bottle's own `git_gate.forges` descriptions, checked; {} when none."""
    git = raw.get("git_gate")
    if git is None:
        return {}
    git_d = as_json_object(git, f"bottle '{bottle_name}' git_gate")
    if "forges" not in git_d:
        return {}
    forges = as_json_object(git_d["forges"], f"bottle '{bottle_name}' git_gate.forges")
    for name, entry in forges.items():
        where = f"bottle '{bottle_name}' git_gate.forges[{name!r}]"
        if "@" in name:
            raise ManifestError(f"{where}: a forge is named by its host; the account an "
                                f"agent pushes as is its `identity.accounts`, not the bottle's")
        if not _FORGE_NAME_RE.match(name):
            raise ManifestError(f"{where}: a forge is named by its host")
        d = as_json_object(entry, where)
        on_host = sorted(set(d) & _HOST_KEYS)
        if on_host:
            raise ManifestError(
                f"{where}.{on_host[0]}: a forge's {', '.join(on_host)} "
                f"{'is' if len(on_host) == 1 else 'are'} the host's connection now, not the "
                f"bottle's; connect it with `{_CONNECT} {_url(name, d)}` and remove "
                f"{'it' if len(on_host) == 1 else 'them'}")
        unknown = set(d) - _FORGE_KEYS
        if unknown:
            raise ManifestError(f"{where} has unknown key {sorted(unknown)[0]!r}; "
                                f"allowed: {', '.join(sorted(_FORGE_KEYS))}")
        kind = _str(d, "kind", where) or "gitea"
        if kind not in FORGE_KINDS:
            raise ManifestError(f"{where}: kind {kind!r} isn't supported yet; "
                                f"supported: {', '.join(FORGE_KINDS)}")
        url = _url(name, d)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ManifestError(f"{where}.url must be an http(s) URL (was {url!r})")
        if _host(url) != name:
            raise ManifestError(f"{where}.url must be on {name}, the forge it names "
                                f"(was {url!r})")
        for key in ("ssh", "host_key"):
            _str(d, key, where)
    return forges


def _url(name: str, d: dict[str, object]) -> str:
    url = d.get("url")
    return (url if isinstance(url, str) and url else f"https://{name}").rstrip("/")


def _host(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "")


def merge_named(earlier: dict[str, object], later: dict[str, object],
                what: str) -> dict[str, object]:
    """Two parents' entries by name. The same name declared differently is an
    error, not a silent pick."""
    for name, entry in later.items():
        if name in earlier and earlier[name] != entry:
            raise ManifestError(f"{what} {name!r} is declared differently by two "
                                f"bottles this one extends; declare it in this bottle "
                                f"to choose, or in only one of them")
    return {**earlier, **later}


def resolve_forge(name: str, described: object, bottle_name: str,
                  user: str = "") -> ManifestForge:
    """The forge `name` as this host connected it, for `user` (else its
    default). `described` is the chain's description of it, or None: its kind
    and pinned host key must match the connection's. Raises ManifestError,
    saying how to connect it, when the host hasn't."""
    # pylint: disable-next=import-outside-toplevel
    from .. import forge_connections
    d = as_json_object(described, f"git_gate.forges[{name!r}]") if described else {}
    where = f"bottle '{bottle_name}'" if bottle_name else "this launch"
    conn = forge_connections.get(name)
    if conn is None:
        raise ManifestError(f"{where}: the forge {name} isn't connected on this host; "
                            f"connect it with `{_CONNECT} {_url(name, d)}`")
    kind = str(d.get("kind") or conn.kind)
    if kind != conn.kind:
        raise ManifestError(f"{where} describes {name} as {kind}, but this host connected it "
                            f"as {conn.kind}")
    pinned = str(d.get("host_key") or "")
    if pinned and conn.host_key and pinned != conn.host_key:
        raise ManifestError(f"{where} pins a host key for {name} that isn't the one this host "
                            f"pinned when it connected it; check the forge, then connect it "
                            f"again or fix the bottle's host_key")
    chosen = conn.user(user)
    return ManifestForge(name=name, kind=conn.kind, url=conn.url, ssh=conn.ssh,
                         token_secret=chosen.token_secret, account=chosen.name,
                         host_key=conn.host_key, app=chosen.app, transport=chosen.transport)


def with_forge_users(bottle: Any, users: dict[str, str]) -> Any:
    """`bottle` (a ManifestBottle) with its forges, and the repos granted on
    them, as the users `users` names by host (an agent's `identity.accounts`)."""
    forges = list(bottle.forges)
    git = list(bottle.git)
    for i, forge in enumerate(forges):
        user = users.get(forge.name)
        if not user or user == forge.account:
            continue
        chosen = resolve_forge(forge.name, None, "", user)
        forges[i] = chosen
        for j, entry in enumerate(git):
            if entry.Key.provider == forge.kind and entry.Key.api_url == forge.url:
                git[j] = replace(entry, Key=replace(entry.Key, forge_token_env=chosen.token_secret,
                                                    transport=chosen.transport))
    return replace(bottle, forges=tuple(forges), git=tuple(git))


def described_forge(forge: ManifestForge) -> dict[str, object]:
    """What a bottle written from `forge` describes it as: its kind, and its
    URL and SSH address unless they're the defaults. The SSH address only
    when it's on the forge's own host (an address over a VPN is this
    machine's, not the forge's)."""
    out: dict[str, object] = {"kind": forge.kind}
    host = forge.name.split(":", 1)[0]
    if forge.url != f"https://{forge.name}":
        out["url"] = forge.url
    ssh_host = forge.ssh.removeprefix("ssh://").split("@", 1)[-1].split(":", 1)[0]
    if ssh_host == host and forge.ssh != f"ssh://git@{host}":
        out["ssh"] = forge.ssh
    return out


def partial_message(undeclared: "frozenset[str] | set[str]", bottles: "list[str]") -> str:
    """Why a launch of `bottles` (names; none for the default) can't go ahead
    when their grants use undeclared forges, and the fix (ADR 0021)."""
    # pylint: disable-next=import-outside-toplevel
    from ..contract.hostctl import RUN_BOTTLE
    hosts = ", ".join(sorted(undeclared))
    one = len(undeclared) == 1
    named = ", ".join(f"'{b}'" for b in bottles) or "the default bottle"
    head = (f"bottle {named} is partial" if len(bottles) <= 1 else f"bottles {named} are partial")
    what = (f"{head}: {'its' if len(bottles) <= 1 else 'their'} grants use {hosts}, and "
            f"nothing declares {'it' if one else 'them'} under `git_gate.forges`.")
    if RUN_BOTTLE in bottles:
        return (f"{what} This run's suggested bottle can't be migrated: launch again so the "
                f"architect writes {'its' if one else 'their'} declaration, or save the "
                f"bottle to the library and migrate it there.")
    target = bottles[-1] if bottles else "<bottle>"
    return (f"{what} Declare {'it' if one else 'them'} (kind, url), or run "
            f"`bot-bottle bottles migrate {target}` to add {'it' if one else 'them'} from "
            f"this host's connections.")


def described_connection(conn: Any) -> dict[str, object]:
    """`described_forge` for a host's connection (`forge_connections`)."""
    return described_forge(ManifestForge(name=conn.host, kind=conn.kind, url=conn.url,
                                         ssh=conn.ssh, token_secret=""))


def split_grant_alias(grant: str) -> tuple[str | None, str]:
    """If `grant` starts with `<alias>=`, return `(alias, bare_grant)`; else
    `(None, grant)`. The alias becomes the repo's local name in git-gate,
    letting two repos with the same upstream name coexist in one bottle."""
    eq = grant.find("=")
    if eq > 0 and _REPO_PART_RE.match(grant[:eq]) and "/" in grant[eq + 1:]:
        return grant[:eq], grant[eq + 1:]
    return None, grant


def parse_grant(grant: str, where: str) -> tuple[str, str, str, bool]:
    """`<forge>/<owner>/<repo>[:write]` as (forge, owner, repo, writable)."""
    body, writable = grant, False
    if grant.endswith(":write"):
        body, writable = grant[:-len(":write")], True
    parts = body.split("/")
    if len(parts) != 3 or not all(parts):
        raise ManifestError(f"{where}: a repo grant is <forge>/<owner>/<repo>[:write] "
                            f"(was {grant!r})")
    forge, owner, repo = parts
    repo = repo.removesuffix(".git")
    if not (_REPO_PART_RE.match(owner) and _REPO_PART_RE.match(repo)):
        raise ManifestError(f"{where}: {grant!r} has an invalid owner or repo name")
    return forge, owner, repo, writable


def normalize_repos(bottle_name: str, raw: dict[str, object]) -> dict[str, object]:
    """`raw` with `git_gate.repos` as a mapping: a list of grants becomes
    `{<repo>: <grant>}`. Other forms are left for the long-form parser."""
    git = raw.get("git_gate")
    if git is None:
        return raw
    git_d = as_json_object(git, f"bottle '{bottle_name}' git_gate")
    repos = git_d.get("repos")
    if not isinstance(repos, list):
        return raw
    mapped: dict[str, object] = {}
    for i, item in enumerate(repos):
        where = f"bottle '{bottle_name}' git_gate.repos[{i}]"
        if not isinstance(item, str):
            raise ManifestError(f"{where}: a list of repos holds grants, "
                                f"<forge>/<owner>/<repo>[:write]")
        repo = parse_grant(item, where)[2]
        if repo in mapped:
            raise ManifestError(f"{where}: two grants for repos named {repo!r}; use the "
                                f"mapping form to name them apart")
        mapped[repo] = item
    return {**raw, "git_gate": {**git_d, "repos": mapped}}


def refuse_minted_long_form(bottle_name: str, repos: dict[str, object]) -> None:
    """A bottle file's own repos, as written: a key a forge mints is declared
    as a grant on that forge, never in long form (ADR 0008). The error names
    every such repo and how to convert them (`manifest/legacy_git_gate.py`)."""
    if old_repos(repos, FORGE_KINDS):
        raise OldGitGateFormatError(bottle_name, repos, FORGE_KINDS)


def granted_forges(bottle_name: str, raw: dict[str, object]) -> set[str]:
    """The forges the bottle's own grants name."""
    git = normalize_repos(bottle_name, raw).get("git_gate")
    repos = git.get("repos") if isinstance(git, dict) else None
    if not isinstance(repos, dict):
        return set()
    return {parse_grant(split_grant_alias(v)[1], f"bottle '{bottle_name}' git_gate.repos")[0]
            for v in repos.values() if isinstance(v, str)}


def expand_grants(bottle_name: str, raw: dict[str, object],
                  forges: dict[str, object]) -> dict[str, object]:
    """`raw` with each grant in `git_gate.repos` replaced by its long form,
    through the host's connection to its forge; `forges` are the descriptions
    of the bottle's whole chain."""
    raw = normalize_repos(bottle_name, raw)
    git = raw.get("git_gate")
    if git is None:
        return raw
    git_d = as_json_object(git, f"bottle '{bottle_name}' git_gate")
    repos = git_d.get("repos")
    if isinstance(repos, dict):
        refuse_minted_long_form(bottle_name, repos)
    if not isinstance(repos, dict) or not any(isinstance(v, str) for v in repos.values()):
        return raw
    expanded: dict[str, object] = {}
    for name, entry in repos.items():
        if not isinstance(entry, str):
            expanded[name] = entry
            continue
        where = f"bottle '{bottle_name}' git_gate.repos[{name!r}]"
        forge_name, owner, repo, writable = parse_grant(entry, where)
        if "@" in forge_name:
            raise ManifestError(f"{where}: a grant names its forge by host; the account an "
                                f"agent pushes as is its `identity.accounts`, not the grant's")
        forge = resolve_forge(forge_name, forges.get(forge_name), bottle_name)
        long_form: dict[str, object] = {
            "url": f"{forge.ssh}/{owner}/{repo}.git", "key": forge.key(writable)}
        if forge.host_key:
            long_form["host_key"] = forge.host_key
        expanded[name] = long_form
    return {**raw, "git_gate": {**git_d, "repos": expanded}}


def strip_chain_keys(raw: dict[str, object]) -> dict[str, object]:
    """`raw` without `credentials` and `git_gate.forges`, which `extends`
    resolves and the bottle parser doesn't read."""
    out = {k: v for k, v in raw.items() if k != "credentials"}
    git = out.get("git_gate")
    if isinstance(git, dict) and "forges" in git:
        out["git_gate"] = {k: v for k, v in git.items() if k != "forges"}
    return out


__all__ = [
    "FORGE_KINDS", "ManifestForge", "credentials_raw", "described_connection", "described_forge",
    "expand_grants", "forges_raw", "granted_forges", "merge_named", "normalize_repos",
    "parse_grant",
    "refuse_minted_long_form", "resolve_forge", "split_grant_alias", "strip_chain_keys",
    "with_forge_users",
]
