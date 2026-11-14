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

A grant's prefix is the forge's real host. On a gitea or github forge the
rest is `<owner>/<repo>`; on an `ssh` forge it's the repo's path on the
server as written, one or more segments (`git.example.com/srv/git/app.git`).
The host's connection to it (`forge_connections`, written by `bot-bottle
forge connect`) supplies the URL, SSH address, pinned host key and token a
launch uses; a bottle's description never overrides them. It's the hint for connecting the forge on a machine
that hasn't, and its `kind` (and `host_key`, if pinned) must match the
connection's. A bottle never carries a token.

A plain SSH git server is a forge of kind `ssh` (ADR 0023): no API, so no
minted keys. The host's connection to it holds key files, one per repo or a
default (`bot-bottle forge key add`), each keyed by the path its grants
name, and a grant uses its repo's key, else the default.

Each grant expands, while `extends` is resolved, into the long-form repo
entry: a deploy key minted per launch through the forge, or an `ssh` forge's
key file, read-only unless `:write`. Everything downstream sees only the
long form. A bottle file never writes the long form itself, whatever its key
(ADR 0008, ADR 0023).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..contract.forges import FORGE_KINDS, FORGE_LABELS, repo_name, repo_path_ok
from .legacy_git_gate import OldGitGateFormatError
from .util import ManifestError, as_json_object

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..forge_connections import ForgeKey
    from .legacy_git_gate import ForgeHome

_FORGE_KEYS = {"kind", "url", "ssh", "host_key"}
# Connection settings that live on the host now (ADR 0019), not in a bottle.
_HOST_KEYS = {"token", "account", "app", "transport"}
# A forge's key: its host, with a port if it has one.
_FORGE_NAME_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_CONNECT = "bot-bottle forge connect"
_KEY_ADD = "bot-bottle forge key add"
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
    # An `ssh` forge's keys on this host (ADR 0023).
    keys: "tuple[ForgeKey, ...]" = ()

    def key(self, writable: bool, repo: str = "", where: str = "") -> dict[str, object]:
        """The long-form `key` block a grant on `repo` (its path, as
        `kind_path` gives it) on this forge expands to. An `ssh` forge's is the repo's key, else its
        default; MissingKeyError (naming `where`) when it has neither, or only
        a read-only one for a `:write` grant."""
        if self.kind == "ssh":
            found = {k.repo: k for k in self.keys}
            chosen = found.get(repo) or found.get("")
            if chosen is None or (writable and not chosen.write):
                raise MissingKeyError(self.name, repo, writable, where,
                                      read_only=chosen is not None)
            return {"provider": "ssh", "path": chosen.path, "fingerprint": chosen.fingerprint,
                    "read_only": not writable}
        key: dict[str, object] = {"provider": self.kind, "forge_token_env": self.token_secret,
                                  "api_url": self.url, "read_only": not writable}
        if self.transport:
            key["transport"] = self.transport
        return key


class NotConnectedError(ManifestError):
    """A grant on a forge this host hasn't connected; the message names the
    `forge connect` that does."""

    def __init__(self, forge: str, message: str) -> None:
        self.forge = forge
        super().__init__(message)


class MissingKeyError(ManifestError):
    """A grant on an `ssh` forge this host holds no usable key for (ADR
    0023): none for its repo and no default, or only a read-only one for a
    `:write` grant. The message names the `forge key add` that fixes it."""

    def __init__(self, forge: str, repo: str, writable: bool, where: str = "", *,
                 read_only: bool = False) -> None:
        self.forge, self.repo, self.writable = forge, repo, writable
        grant = f"{forge}/{repo}"
        fix = f"`{_KEY_ADD} {forge} {repo} <path>{' --write' if writable else ''}`"
        prefix = f"{where}: " if where else ""
        if read_only:
            what = (f"{prefix}this host's key for {grant} is read-only, and the grant is "
                    f":write; add a write key with {fix}")
        else:
            what = (f"{prefix}this host holds no key for {grant}; add one with {fix} "
                    f"(or `{_KEY_ADD} {forge} --default <path>` for every repo there)")
        super().__init__(what)


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
        if not _FORGE_NAME_RE.fullmatch(name):
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
        for key in ("ssh", "host_key"):
            _str(d, key, where)
        if kind == "ssh":
            _ssh_described(name, d, where)
            continue
        url = _url(name, d)
        try:
            # urlparse refuses a bad IPv6 host, `.port` a port that isn't one.
            parsed = urlparse(url)
            host = _host(url)
        except ValueError:
            raise ManifestError(f"{where}.url must be an http(s) URL with a valid "
                                f"host and port (was {url!r})") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ManifestError(f"{where}.url must be an http(s) URL (was {url!r})")
        if host != name:
            raise ManifestError(f"{where}.url must be on {name}, the forge it names "
                                f"(was {url!r})")
    return forges


_SSH_RE = re.compile(r"^ssh://[^@/\s]+@([A-Za-z0-9.-]+)(?::[0-9]+)?$")


def _ssh_described(name: str, d: dict[str, object], where: str) -> None:
    """An `ssh` forge's description: no `url` (it has no web side), and an
    `ssh` address on the host it names, the port its own."""
    if "url" in d:
        raise ManifestError(f"{where}: an SSH server has no url; give its address as "
                            f"ssh: ssh://git@{name}[:port]")
    if ":" in name:
        raise ManifestError(f"{where}: an SSH server is named by its host alone; its port "
                            f"goes in ssh: ssh://git@<host>:<port>")
    ssh = str(d.get("ssh") or "")
    m = _SSH_RE.fullmatch(ssh) if ssh else None
    if ssh and (m is None or m.group(1).lower() != name.lower()):
        raise ManifestError(f"{where}.ssh must be ssh://user@{name}[:port] (was {ssh!r})")


def _url(name: str, d: dict[str, object]) -> str:
    """Where `forge connect` connects a forge described as `d`: an SSH
    server's `ssh` address, else its url."""
    if d.get("kind") == "ssh":
        ssh = d.get("ssh")
        return ssh if isinstance(ssh, str) and ssh else f"ssh://git@{name}"
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
        raise NotConnectedError(name, f"{where}: the forge {name} isn't connected on this "
                                      f"host; connect it with `{_CONNECT} {_url(name, d)}`")
    kind = str(d.get("kind") or conn.kind)
    if kind != conn.kind:
        raise ManifestError(f"{where} describes {name} as {kind}, but this host connected it "
                            f"as {conn.kind}")
    pinned = str(d.get("host_key") or "")
    if pinned and conn.host_key and pinned != conn.host_key:
        raise ManifestError(f"{where} pins a host key for {name} that isn't the one this host "
                            f"pinned when it connected it; check the forge, then connect it "
                            f"again or fix the bottle's host_key")
    if conn.kind == "ssh":
        if user:
            raise ManifestError(f"{name} is an SSH server: it has keys, not users; take it "
                                f"out of the agent's identity.accounts")
        return ManifestForge(name=name, kind="ssh", url="", ssh=conn.ssh, token_secret="",
                             host_key=conn.host_key, keys=conn.keys)
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
    machine's, not the forge's). An SSH server has no URL."""
    out: dict[str, object] = {"kind": forge.kind}
    host = forge.name.split(":", 1)[0]
    if forge.kind != "ssh" and forge.url != f"https://{forge.name}":
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
    if bottles == [RUN_BOTTLE]:
        return (f"{what} This run's suggested bottle can't be migrated: launch again so the "
                f"architect writes {'its' if one else 'their'} declaration, or save the "
                f"bottle to the library and migrate it there.")
    target = bottles[0] if len(bottles) == 1 else "<bottle>"
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
    if eq > 0 and _REPO_PART_RE.fullmatch(grant[:eq]) and "/" in grant[eq + 1:]:
        return grant[:eq], grant[eq + 1:]
    return None, grant


def grant_path(grant: str, where: str) -> tuple[str, str, bool]:
    """`<forge>/<path>[:write]` as (forge, path, writable), the path as
    written. Any forge's grant reads this way; `kind_path` then holds a
    gitea or github grant to `<owner>/<repo>` (ADR 0023)."""
    body, writable = grant, False
    if grant.endswith(":write"):
        body, writable = grant[:-len(":write")], True
    parts = body.split("/")
    if len(parts) < 2 or not all(parts):
        raise ManifestError(f"{where}: a repo grant is <forge>/<owner>/<repo>[:write], or "
                            f"<forge>/<path>[:write] on an SSH server (was {grant!r})")
    path = "/".join(parts[1:])
    if not repo_path_ok(path):
        raise ManifestError(f"{where}: {grant!r} has an invalid owner or repo name")
    return parts[0], path, writable


def parse_grant(grant: str, where: str) -> tuple[str, str, str, bool]:
    """A grant as (forge, owner, repo, writable): `repo` is the gate's local
    name (the path's last segment without `.git`), `owner` the segments
    before it ("" for a one-segment path on an SSH server)."""
    forge, path, writable = grant_path(grant, where)
    return forge, path.rpartition("/")[0], repo_name(path), writable


def kind_path(kind: str, path: str, grant: str, where: str) -> str:
    """The path a grant's repo has on a forge of `kind`, as its key and URL
    use it: an `ssh` forge's as written (nothing added or taken off); a
    gitea or github forge's `<owner>/<repo>` without `.git`, refused when
    `path` isn't two segments."""
    if kind == "ssh":
        return path
    if path.count("/") != 1:
        raise ManifestError(f"{where}: a {FORGE_LABELS.get(kind, kind)} repo grant is "
                            f"<forge>/<owner>/<repo>[:write]; only an SSH server's (kind: ssh) "
                            f"names any other path (was {grant!r})")
    return f"{path.split('/')[0]}/{repo_name(path)}"


def repo_url(forge: ManifestForge, path: str) -> str:
    """The upstream URL of the repo at `path` (`kind_path`'s) on `forge`."""
    return f"{forge.ssh}/{path}" if forge.kind == "ssh" else f"{forge.ssh}/{path}.git"


def check_grant_paths(bottle_name: str, raw: dict[str, object],
                      forges: "Mapping[str, object]") -> None:
    """Each grant on a forge `forges` describes as a gitea or github one (the
    default kind) names `<owner>/<repo>`; an `ssh` forge's may name any path.
    A grant on an undescribed forge is checked once its connection says."""
    git = normalize_repos(bottle_name, raw).get("git_gate")
    repos = git.get("repos") if isinstance(git, dict) else None
    for name, entry in (repos.items() if isinstance(repos, dict) else ()):
        if not isinstance(entry, str):
            continue
        where = f"bottle '{bottle_name}' git_gate.repos[{name!r}]"
        forge, path, _ = grant_path(split_grant_alias(entry)[1], where)
        described = forges.get(forge)
        if isinstance(described, dict):
            kind = described.get("kind") or "gitea"
            kind_path(kind if isinstance(kind, str) else "gitea", path, entry, where)


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
                                f"<forge>/<owner>/<repo>[:write] (<forge>/<path> on an SSH "
                                f"server)")
        repo = parse_grant(item, where)[2]
        if repo in mapped:
            raise ManifestError(f"{where}: two grants for repos named {repo!r}; use the "
                                f"mapping form to name them apart")
        mapped[repo] = item
    return {**raw, "git_gate": {**git_d, "repos": mapped}}


def refuse_long_form(bottle_name: str, repos: "Mapping[str, object]", *,
                     homes: "Mapping[str, ForgeHome] | None" = None) -> None:
    """A bottle file's own repos, as written: each is a grant, never the long
    form, whatever its key (ADR 0008, ADR 0023). The error names every such
    repo, the grant it becomes and the commands that connect it
    (`manifest/legacy_git_gate.py`). `homes`: the connections to plan with,
    {} to ask none (a check that mustn't depend on this host)."""
    if any(not isinstance(v, str) for v in repos.values()):
        raise OldGitGateFormatError(bottle_name, repos, homes=homes)


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
        refuse_long_form(bottle_name, repos)
        check_grant_paths(bottle_name, raw, forges)
    if not isinstance(repos, dict) or not any(isinstance(v, str) for v in repos.values()):
        return raw
    expanded: dict[str, object] = {}
    for name, entry in repos.items():
        if not isinstance(entry, str):
            expanded[name] = entry
            continue
        where = f"bottle '{bottle_name}' git_gate.repos[{name!r}]"
        forge_name, path, writable = grant_path(entry, where)
        if "@" in forge_name:
            raise ManifestError(f"{where}: a grant names its forge by host; the account an "
                                f"agent pushes as is its `identity.accounts`, not the grant's")
        forge = resolve_forge(forge_name, forges.get(forge_name), bottle_name)
        path = kind_path(forge.kind, path, entry, where)
        long_form: dict[str, object] = {
            "url": repo_url(forge, path),
            "key": forge.key(writable, path, f"bottle '{bottle_name}'")}
        if forge.host_key:
            long_form["host_key"] = forge.host_key
        expanded[name] = long_form
    return {**raw, "git_gate": {**git_d, "repos": expanded}}


def refuse_ssh_create(bottle_name: str, raw: dict[str, object],
                      forges: dict[str, object]) -> None:
    """`git_gate.actions.create` on a forge the chain describes as an SSH
    server is refused: creating a repo needs a forge API (ADR 0023)."""
    git = raw.get("git_gate")
    actions = git.get("actions") if isinstance(git, dict) else None
    create = actions.get("create") if isinstance(actions, dict) else None
    for pattern in create if isinstance(create, list) else []:
        forge = str(pattern).split("/", 1)[0]
        described = forges.get(forge)
        if isinstance(described, dict) and described.get("kind") == "ssh":
            raise ManifestError(f"bottle '{bottle_name}' git_gate.actions.create: {forge} is "
                                f"an SSH server, and creating a repo needs a forge's API")


def strip_chain_keys(raw: dict[str, object]) -> dict[str, object]:
    """`raw` without `credentials` and `git_gate.forges`, which `extends`
    resolves and the bottle parser doesn't read."""
    out = {k: v for k, v in raw.items() if k != "credentials"}
    git = out.get("git_gate")
    if isinstance(git, dict) and "forges" in git:
        out["git_gate"] = {k: v for k, v in git.items() if k != "forges"}
    return out


__all__ = [
    "FORGE_KINDS", "ManifestForge", "MissingKeyError", "NotConnectedError", "check_grant_paths",
    "credentials_raw", "described_connection", "described_forge", "expand_grants", "forges_raw",
    "grant_path", "granted_forges", "kind_path", "merge_named", "normalize_repos", "parse_grant",
    "refuse_long_form", "refuse_ssh_create", "repo_url", "resolve_forge", "split_grant_alias",
    "strip_chain_keys", "with_forge_users",
]
