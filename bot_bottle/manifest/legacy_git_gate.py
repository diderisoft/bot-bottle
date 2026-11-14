"""The old git-gate format, and the way out of it (ADR 0008, ADR 0023).

Before forges, a bottle wrote each repo in long form, with its own `url`,
`key` and `host_key`: a key a forge minted (`provider: gitea`,
`forge_token_env`, `api_url`), or an operator's own key file (`provider:
static`, `path`). That form no longer loads, whatever the key: every repo
in a bottle file is a grant, `<forge>/<owner>/<repo>[:write]`, on a forge
this host has connected (`bot-bottle forge connect`, ADR 0019). A static
key's server is a forge of kind `ssh`, and its key is the connection's
(`bot-bottle forge key add`).

This module finds every such repo in a bottle at once, works out the grant
each becomes, and explains the fix: connect the forge (and add an SSH
server's keys), then `bot-bottle bottles migrate <bottle>`
(`manifest/migrate.py`).
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import ParseResult, urlparse

from ..contract.forges import API_KINDS
from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from .util import ManifestError

DOCS = "docs/reference/git-gate.md#migrating-from-the-old-format"


@dataclass(frozen=True)
class OldRepo:
    """One long-form repo."""

    name: str          # the gate's repo name (its key under git_gate.repos)
    upstream: str      # ssh://user@host[:port]/owner/repo.git
    forge_url: str     # a minted key's forge, scheme://host[:port]; "" for a static key
    ssh: str           # the upstream's ssh://user@host[:port]
    owner: str         # "" when the repo can't be converted (see `convertible`)
    repo: str
    writable: bool     # the old key could push unless `read_only: true`
    token_env: str     # the host variable its token came from (a name)
    host_key: str = ""
    # The old key's provider: an API kind, `static`, or anything else
    # (which can only be rewritten by hand).
    provider: str = ""
    key_path: str = ""  # a static key's file

    @property
    def static(self) -> bool:
        """An operator's own key file (or the long form an `ssh` forge's grant
        expands to, copied into a file): its server becomes an `ssh` forge."""
        return self.provider in ("static", "ssh")

    @property
    def forge_host(self) -> str:
        """The forge's key, as `forge connect` names it: a minted key's
        forge's host[:port], or a static key's server's host (its SSH port
        is the forge's `ssh`)."""
        if self.static:
            return (_urlparse(self.upstream).hostname or "").lower()
        return urlparse(self.forge_url).netloc.lower()

    def grant(self, forge_key: str = "") -> str:
        return (f"{forge_key or self.forge_host}/{self.owner}/{self.repo}"
                + (":write" if self.writable else ""))


def old_repos(repos: Mapping[str, object]) -> list[OldRepo]:
    """The entries of a bottle's own `git_gate.repos` (a mapping) written in
    long form: every one that isn't a grant."""
    out: list[OldRepo] = []
    for name, entry in repos.items():
        if isinstance(entry, str):
            continue
        d = entry if isinstance(entry, dict) else {}
        found = d.get("key")
        key: dict[str, object] = found if isinstance(found, dict) else {}
        provider = str(key.get("provider") or "")
        upstream = str(d.get("url") or "")
        parsed = _urlparse(upstream)
        api = _urlparse(str(key.get("api_url") or ""))
        # The old default: the API on the upstream's host (git_gate/provision.py).
        forge_url = "" if provider not in API_KINDS else (
            f"{api.scheme}://{api.netloc}" if api.scheme and api.netloc
            else f"https://{parsed.hostname or ''}")
        parts = parsed.path.strip("/").split("/")
        owner, repo = (parts[0], parts[1].removesuffix(".git")) if (
            len(parts) == 2 and all(parts) and parsed.scheme == "ssh" and parsed.hostname
            and (provider in API_KINDS or (provider in ("static", "ssh") and key.get("path")))
        ) else ("", "")
        out.append(OldRepo(
            name=name, upstream=upstream, forge_url=forge_url,
            ssh=f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "",
            owner=owner, repo=repo,
            writable=key.get("read_only") is not True,
            token_env=str(key.get("forge_token_env") or ""),
            host_key=str(d.get("host_key") or ""), provider=provider,
            key_path=str(key.get("path") or "")))
    return out


def _urlparse(url: str) -> ParseResult:
    """`url` parsed, or nothing for one urlparse refuses (a bad IPv6 host):
    the repo is still old form, just one to rewrite by hand."""
    try:
        return urlparse(url)
    except ValueError:
        return urlparse("")


@dataclass(frozen=True)
class ForgeHome:
    """A forge this host connected (`forge_connections`), as a plan sees it."""

    key: str            # its host, a grant's prefix
    ssh: str = ""
    host_key: str = ""
    token_secrets: tuple[str, ...] = ()
    kind: str = ""
    # An SSH server's keys: the repos it holds one for ("" the default).
    key_repos: tuple[str, ...] = ()


def connected_homes() -> dict[str, ForgeHome]:
    """The forges this host connected, by host."""
    # pylint: disable-next=import-outside-toplevel
    from .. import forge_connections
    try:
        found = forge_connections.connections()
    except ManifestError:
        return {}
    return {host: ForgeHome(key=host, ssh=c.ssh, host_key=c.host_key,
                            token_secrets=tuple(u.token_secret for u in c.users),
                            kind=c.kind, key_repos=tuple(k.repo for k in c.keys))
            for host, c in found.items()}


def scan_bottles(dirs: Sequence[tuple[Path, bool]]) -> list[tuple[str, dict[str, object]]]:
    """Every bottle file in `dirs` (precedence order) as `(name, frontmatter)`,
    a name's first file only; files that don't parse are skipped."""
    seen: set[str] = set()
    out: list[tuple[str, dict[str, object]]] = []
    for d, project in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            name = ("./" if project else "") + path.stem
            if name in seen:
                continue
            seen.add(name)
            try:
                fm, _ = parse_frontmatter(path.read_text())
            except (OSError, YamlSubsetError):
                continue
            out.append((name, dict(fm)))
    return out


@dataclass(frozen=True)
class Plan:
    """How one bottle's old repos become grants."""

    bottle: str
    repos: tuple[OldRepo, ...]
    grants: dict[str, str]                     # repo name -> grant
    missing: dict[str, tuple[OldRepo, ...]]    # forge host -> repos on a forge not connected
    homes: dict[str, ForgeHome]                # forge host -> its connection
    unconvertible: tuple[OldRepo, ...]         # upstream path isn't <owner>/<repo>

    def connect_command(self, host: str) -> str:
        repos = self.missing[host]
        if repos[0].static:
            # The bottle's own pin carries over, rather than a fresh scan.
            pinned = next((r.host_key for r in repos if r.host_key), "")
            return (f"bot-bottle forge connect {repos[0].ssh}"
                    + (f" --host-key {shlex.quote(pinned)}" if pinned else ""))
        cmd = f"bot-bottle forge connect {repos[0].forge_url}"
        ssh = repos[0].ssh
        if ssh and ssh != f"ssh://git@{urlparse(repos[0].forge_url).hostname}":
            cmd += f" --ssh {ssh}"
        return cmd

    def servers(self) -> dict[str, tuple[OldRepo, ...]]:
        """The converted static repos, by the SSH server each becomes. A
        server this host connected as another kind of forge (a Gitea's SSH
        side) isn't one: its grants use that forge."""
        out: dict[str, list[OldRepo]] = {}
        for r in self.repos:
            home = self.homes.get(r.forge_host)
            if r.static and r.owner and (home is None or home.kind in ("", "ssh")):
                out.setdefault(r.forge_host, []).append(r)
        return {h: tuple(rs) for h, rs in out.items()}

    def key_commands(self) -> list[str]:
        """`forge key add` for each static repo whose server holds no key for
        it here yet: the key file it named, with write as it had."""
        out: list[str] = []
        for host, repos in self.servers().items():
            home = self.homes.get(host)
            held = set(home.key_repos) if home else set()
            for r in repos:
                if f"{r.owner}/{r.repo}" not in held and "" not in held:
                    out.append(f"bot-bottle forge key add {host} {r.owner}/{r.repo} "
                               f"{r.key_path} --write")
        return out

    def declarations(self) -> dict[str, dict[str, object]]:
        """The `ssh` forge each static repo's server becomes (ADR 0023), from
        its url and host_key, by host."""
        out: dict[str, dict[str, object]] = {}
        for host, repos in self.servers().items():
            d: dict[str, object] = {"kind": "ssh"}
            if repos[0].ssh != f"ssh://git@{host}":
                d["ssh"] = repos[0].ssh
            pinned = next((r.host_key for r in repos if r.host_key), "")
            if pinned:
                d["host_key"] = pinned
            out[host] = d
        return out


def plan(bottle: str, repos: Sequence[OldRepo], homes: Mapping[str, ForgeHome]) -> Plan:
    """`bottle`'s old repos as grants, on the forges `homes` (this host's
    connections, by host)."""
    by_host: dict[str, list[OldRepo]] = {}
    for r in repos:
        if r.owner:
            by_host.setdefault(r.forge_host, []).append(r)
    found = {h: homes[h] for h in by_host if h in homes}
    missing = {h: tuple(rs) for h, rs in by_host.items() if h not in homes}
    grants = {r.name: r.grant() for r in repos if r.owner}
    return Plan(bottle=bottle, repos=tuple(repos), grants=grants, missing=missing,
                homes=found, unconvertible=tuple(r for r in repos if not r.owner))


def flow_list(names: Sequence[str]) -> str:
    """One name bare, several as a flow list."""
    return names[0] if len(names) == 1 else "[" + ", ".join(names) + "]"


def list_form(p: Plan, repos: Mapping[str, object]) -> bool:
    """Whether the repos can be written as a list of grants: every one is
    converted and named after its repo."""
    return (len(p.grants) == len(repos)
            and all(name == p.grants[name].removesuffix(":write").rsplit("/", 1)[1]
                    for name in repos if name in p.grants))


def hand_edit(p: Plan, repos: Mapping[str, object]) -> str:
    """The frontmatter lines to write by hand, for the repos the plan converts."""
    lines = ["git_gate:"]
    declared = p.declarations()
    if declared:
        lines.append("  forges:")
        for host, d in declared.items():
            lines += [f"    {host}:", *(f"      {k}: {v}" for k, v in d.items())]
    lines.append("  repos:")
    if list_form(p, repos):
        lines += [f"    - {g}" for g in p.grants.values()]
    else:
        lines += [f"    {name}: {grant}" for name, grant in p.grants.items()]
        kept = [n for n in repos if n not in p.grants]
        if kept:
            lines.append(f"    # ...and {', '.join(kept)} as they are")
    return "\n".join(lines)


def explain(p: Plan, repos: Mapping[str, object]) -> str:
    """Why `p.bottle` doesn't load, and the steps that fix it."""
    names = ", ".join(r.name for r in p.repos)
    lines = [f"bottle '{p.bottle}' uses the old git-gate format: its repos are written in "
             f"long form ({names}). A bottle's repos are one-line grants on a forge this "
             f"host connects; a plain SSH server is a forge of kind ssh, its keys this "
             f"host's."]
    step = 1
    for host in p.missing:
        what = "SSH server" if p.missing[host][0].static else "forge"
        lines.append(f"  {step}. Connect the {what} once:  {p.connect_command(host)}")
        step += 1
    for host in p.homes:
        lines.append(f"  {step}. {host} is connected already.")
        step += 1
    for cmd in p.key_commands():
        lines.append(f"  {step}. Add its key:  {cmd}")
        step += 1
    lines.append(f"  {step}. Convert this bottle:      bot-bottle bottles migrate {p.bottle}")
    if p.grants:
        lines.append("Or edit it by hand to:\n" + _indent(hand_edit(p, repos), "    "))
    for r in p.unconvertible:
        why = (f"{r.upstream} isn't <owner>/<repo> on the forge"
               if r.provider in (*API_KINDS, "static", "ssh")
               else f"its key provider {r.provider or '(none)'} isn't one bot-bottle knows")
        lines.append(f"  {r.name}: {why}; rewrite it by hand")
    lines.append(f"See {DOCS}")
    return "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


class OldGitGateFormatError(ManifestError):
    """A bottle file still writes repos in long form."""

    def __init__(self, bottle: str, repos: Mapping[str, object], *,
                 homes: Mapping[str, ForgeHome] | None = None) -> None:
        self.bottle = bottle
        self.raw_repos = dict(repos)
        self.plan = plan(bottle, old_repos(repos),
                         connected_homes() if homes is None else homes)
        super().__init__(explain(self.plan, repos))


__all__ = ["DOCS", "ForgeHome", "OldGitGateFormatError", "OldRepo", "Plan", "connected_homes",
           "explain", "flow_list", "hand_edit", "list_form", "old_repos", "plan", "scan_bottles"]
