"""The old git-gate format, and the way out of it (ADR 0008).

Before forges, a bottle wrote each forge-minted repo in long form, with its
own `url`, `key: {provider: gitea, forge_token_env, api_url}` and
`host_key`. That form no longer loads: a repo on a forge is a grant,
`<forge>/<owner>/<repo>[:write]`, on a forge this host has connected
(`bot-bottle forge connect`, ADR 0019).

This module finds every such repo in a bottle at once, works out the grant
each becomes, and explains the fix: connect the forge, then
`bot-bottle bottles migrate <bottle>` (`manifest/migrate.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
from .util import ManifestError

DOCS = "docs/reference/git-gate.md#migrating-from-the-old-format"


@dataclass(frozen=True)
class OldRepo:
    """One long-form repo whose key a forge mints."""

    name: str          # the gate's repo name (its key under git-gate.repos)
    upstream: str      # ssh://user@host[:port]/owner/repo.git
    forge_url: str     # the forge's web/API address, scheme://host[:port]
    ssh: str           # the upstream's ssh://user@host[:port]
    owner: str         # "" when the upstream path isn't <owner>/<repo>
    repo: str
    writable: bool     # the old key could push unless `read_only: true`
    token_env: str     # the host variable its token came from (a name)
    host_key: str = ""

    @property
    def forge_host(self) -> str:
        """The forge's host[:port], which `forge connect` names it by."""
        return urlparse(self.forge_url).netloc.lower()

    def grant(self, forge_key: str = "") -> str:
        return (f"{forge_key or self.forge_host}/{self.owner}/{self.repo}"
                + (":write" if self.writable else ""))


def old_repos(repos: Mapping[str, object], kinds: Sequence[str]) -> list[OldRepo]:
    """The entries of a bottle's own `git-gate.repos` (a mapping) written in
    long form with a key minted by one of `kinds`."""
    out: list[OldRepo] = []
    for name, entry in repos.items():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, dict) or key.get("provider") not in kinds:
            continue
        upstream = str(entry.get("url") or "")
        parsed = urlparse(upstream)
        api = urlparse(str(key.get("api_url") or ""))
        # The old default: the API on the upstream's host (git_gate/provision.py).
        forge_url = (f"{api.scheme}://{api.netloc}" if api.scheme and api.netloc
                     else f"https://{parsed.hostname or ''}")
        parts = parsed.path.strip("/").split("/")
        owner, repo = (parts[0], parts[1].removesuffix(".git")) if (
            len(parts) == 2 and all(parts)) else ("", "")
        out.append(OldRepo(
            name=name, upstream=upstream, forge_url=forge_url,
            ssh=f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "",
            owner=owner, repo=repo, writable=key.get("read_only") is not True,
            token_env=str(key.get("forge_token_env") or ""),
            host_key=str(entry.get("host_key") or "")))
    return out


@dataclass(frozen=True)
class ForgeHome:
    """A forge this host connected (`forge_connections`), as a plan sees it."""

    key: str            # its host, a grant's prefix
    ssh: str = ""
    host_key: str = ""
    token_secrets: tuple[str, ...] = ()


def connected_homes() -> dict[str, ForgeHome]:
    """The forges this host connected, by host."""
    # pylint: disable-next=import-outside-toplevel
    from .. import forge_connections
    try:
        found = forge_connections.connections()
    except ManifestError:
        return {}
    return {host: ForgeHome(key=host, ssh=c.ssh, host_key=c.host_key,
                            token_secrets=tuple(u.token_secret for u in c.users))
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
        cmd = f"bot-bottle forge connect {repos[0].forge_url}"
        ssh = repos[0].ssh
        if ssh and ssh != f"ssh://git@{urlparse(repos[0].forge_url).hostname}":
            cmd += f" --ssh {ssh}"
        return cmd


def plan(bottle: str, repos: Sequence[OldRepo], homes: Mapping[str, ForgeHome]) -> Plan:
    """`bottle`'s old repos as grants, on the forges `homes` (this host's
    connections, by host)."""
    by_host: dict[str, list[OldRepo]] = {}
    for r in repos:
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
    lines = ["git-gate:", "  repos:"]
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
    lines = [f"bottle '{p.bottle}' uses the old git-gate format: its repos mint their own "
             f"keys ({names}). Repos are now one-line grants on a forge this host connects."]
    step = 1
    for host in p.missing:
        lines.append(f"  {step}. Connect the forge once:  {p.connect_command(host)}")
        step += 1
    for host in p.homes:
        lines.append(f"  {step}. {host} is connected already.")
        step += 1
    lines.append(f"  {step}. Convert this bottle:      bot-bottle bottles migrate {p.bottle}")
    if p.grants:
        lines.append("Or edit it by hand to:\n" + _indent(hand_edit(p, repos), "    "))
    for r in p.unconvertible:
        lines.append(f"  {r.name}: {r.upstream} isn't <owner>/<repo> on the forge; "
                     f"rewrite it by hand")
    lines.append(f"See {DOCS}")
    return "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


class OldGitGateFormatError(ManifestError):
    """A bottle file still writes forge-minted repos in long form."""

    def __init__(self, bottle: str, repos: Mapping[str, object], kinds: Sequence[str], *,
                 homes: Mapping[str, ForgeHome] | None = None) -> None:
        self.bottle = bottle
        self.raw_repos = dict(repos)
        self.kinds = tuple(kinds)
        self.plan = plan(bottle, old_repos(repos, kinds),
                         connected_homes() if homes is None else homes)
        super().__init__(explain(self.plan, repos))


__all__ = ["DOCS", "ForgeHome", "OldGitGateFormatError", "OldRepo", "Plan", "connected_homes",
           "explain", "flow_list", "hand_edit", "list_form", "old_repos", "plan", "scan_bottles"]
