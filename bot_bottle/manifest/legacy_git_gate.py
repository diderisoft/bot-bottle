"""The old git-gate format, and the way out of it (ADR 0008).

Before forges, a bottle wrote each forge-minted repo in long form, with its
own `url`, `key: {provider: gitea, forge_token_env, api_url}` and
`host_key`. That form no longer loads: a repo on a forge is a grant,
`<forge>/<owner>/<repo>[:write]`, on a forge some bottle in the chain
declares (usually the one `bot-bottle forge connect` writes).

This module finds every such repo in a bottle at once, works out the grant
each becomes, and explains the fix: connect the forge, then
`bot-bottle bottles migrate <bottle>` (`manifest/migrate.py`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
    """Where a forge is declared: `bottle` ("" for the bottle's own chain) and
    its key there."""

    bottle: str
    key: str
    ssh: str = ""
    host_key: str = ""
    token_secret: str = ""


def forges_by_host(bottles: Iterable[tuple[str, Mapping[str, object]]]
                   ) -> dict[str, list[ForgeHome]]:
    """Each forge host (host[:port]) and the bottles declaring it, from
    `(bottle, frontmatter)` pairs, in the order given."""
    out: dict[str, list[ForgeHome]] = {}
    for bottle, fm in bottles:
        for key, forge in _forges_in(fm).items():
            url = forge.get("url")
            if not isinstance(url, str):
                continue
            host = urlparse(url.rstrip("/")).netloc.lower()
            out.setdefault(host, []).append(ForgeHome(
                bottle=bottle, key=key, ssh=str(forge.get("ssh") or ""),
                host_key=str(forge.get("host_key") or ""),
                token_secret=_token_secret(fm, forge)))
    return out


def _forges_in(fm: Mapping[str, object]) -> dict[str, dict[str, object]]:
    git = fm.get("git-gate")
    forges = git.get("forges") if isinstance(git, dict) else None
    if not isinstance(forges, dict):
        return {}
    return {str(k): v for k, v in forges.items() if isinstance(v, dict)}


def _token_secret(fm: Mapping[str, object], forge: Mapping[str, object]) -> str:
    token = forge.get("token")
    creds = fm.get("credentials")
    if not (isinstance(token, str) and token.startswith("$") and isinstance(creds, dict)):
        return ""
    cred = creds.get(token[1:])
    return str(cred.get("secret") or "") if isinstance(cred, dict) else ""


def pick_forge(homes: Sequence[ForgeHome], repos: Sequence[OldRepo]) -> ForgeHome | None:
    """The forge entry the repos should be granted on: the one whose token is
    the secret they minted with, else the one named by the bare host, else the
    first."""
    if not homes:
        return None
    envs = {r.token_env for r in repos}
    host = repos[0].forge_host if repos else ""
    return (next((h for h in homes if h.token_secret and h.token_secret in envs), None)
            or next((h for h in homes if h.key == host), None)
            or homes[0])


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


def extends_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


@dataclass(frozen=True)
class Plan:
    """How one bottle's old repos become grants."""

    bottle: str
    repos: tuple[OldRepo, ...]
    grants: dict[str, str]                     # repo name -> grant
    add_extends: tuple[str, ...]               # forge bottles to extend
    missing: dict[str, tuple[OldRepo, ...]]    # forge host -> repos with no forge bottle
    homes: dict[str, ForgeHome]                # forge host -> where it's declared
    unconvertible: tuple[OldRepo, ...]         # upstream path isn't <owner>/<repo>

    def connect_command(self, host: str) -> str:
        repos = self.missing[host]
        cmd = f"bot-bottle forge connect {repos[0].forge_url}"
        ssh = repos[0].ssh
        if ssh and ssh != f"ssh://git@{urlparse(repos[0].forge_url).hostname}":
            cmd += f" --ssh {ssh}"
        return cmd


def plan(bottle: str, repos: Sequence[OldRepo], extends: object,
         chain_hosts: Mapping[str, ForgeHome],
         forge_bottles: Mapping[str, Sequence[ForgeHome]]) -> Plan:
    """`bottle`'s old repos as grants. `chain_hosts` are forges its chain
    already declares (by host); `forge_bottles` every bottle declaring each."""
    by_host: dict[str, list[OldRepo]] = {}
    for r in repos:
        by_host.setdefault(r.forge_host, []).append(r)
    current = extends_list(extends)
    homes: dict[str, ForgeHome] = {}
    missing: dict[str, tuple[OldRepo, ...]] = {}
    add: list[str] = []
    for host, host_repos in by_host.items():
        home = chain_hosts.get(host) or pick_forge(
            [h for h in forge_bottles.get(host, ()) if h.bottle != bottle], host_repos)
        if home is None:
            missing[host] = tuple(host_repos)
            continue
        homes[host] = home
        if home.bottle and home.bottle not in current and home.bottle not in add:
            add.append(home.bottle)
    grants = {r.name: r.grant(homes[r.forge_host].key if r.forge_host in homes else "")
              for r in repos if r.owner}
    return Plan(bottle=bottle, repos=tuple(repos), grants=grants, add_extends=tuple(add),
                missing=missing, homes=homes,
                unconvertible=tuple(r for r in repos if not r.owner))


def flow_list(names: Sequence[str]) -> str:
    """One name bare, several as a flow list."""
    return names[0] if len(names) == 1 else "[" + ", ".join(names) + "]"


def new_extends(extends: object, add: Sequence[str]) -> object:
    """`extends` with the forge bottles first (so the bottle's own parents,
    later in the list, still win), as `migrate` writes it."""
    if not add:
        return extends
    current = extends_list(extends)
    if not current and len(add) == 1:
        return add[0]
    return [*add, *current]


def list_form(p: Plan, repos: Mapping[str, object]) -> bool:
    """Whether the repos can be written as a list of grants: every one is
    converted and named after its repo."""
    return (len(p.grants) == len(repos)
            and all(name == p.grants[name].removesuffix(":write").rsplit("/", 1)[1]
                    for name in repos if name in p.grants))


def hand_edit(p: Plan, extends: object, repos: Mapping[str, object]) -> str:
    """The frontmatter lines to write by hand, for the repos the plan converts."""
    lines: list[str] = []
    add = list(p.add_extends)
    if p.missing and "gitea" not in extends_list(extends):
        add.append("gitea")  # the bottle `forge connect` writes by default
    ext = new_extends(extends, add)
    if ext:
        lines.append(f"extends: {flow_list(extends_list(ext))}")
    lines += ["git-gate:", "  repos:"]
    if list_form(p, repos):
        lines += [f"    - {g}" for g in p.grants.values()]
    else:
        lines += [f"    {name}: {grant}" for name, grant in p.grants.items()]
        kept = [n for n in repos if n not in p.grants]
        if kept:
            lines.append(f"    # ...and {', '.join(kept)} as they are")
    return "\n".join(lines)


def explain(p: Plan, extends: object, repos: Mapping[str, object]) -> str:
    """Why `p.bottle` doesn't load, and the steps that fix it."""
    names = ", ".join(r.name for r in p.repos)
    lines = [f"bottle '{p.bottle}' uses the old git-gate format: its repos mint their own "
             f"keys ({names}). Repos now come from a forge bottle, as one-line grants."]
    step = 1
    for host in p.missing:
        lines.append(f"  {step}. Connect the forge once:  {p.connect_command(host)}")
        step += 1
    for host, home in p.homes.items():
        where = f"bottle '{home.bottle}'" if home.bottle else "this bottle's chain"
        lines.append(f"  {step}. {host} is connected already, as {home.key!r} in {where}.")
        step += 1
    lines.append(f"  {step}. Convert this bottle:      bot-bottle bottles migrate {p.bottle}")
    if p.grants:
        lines.append("Or edit it by hand to:\n" + _indent(hand_edit(p, extends, repos), "    "))
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
                 chain_forges: Mapping[str, object] | None = None,
                 extends: object = None,
                 forge_bottles: Mapping[str, Sequence[ForgeHome]] | None = None) -> None:
        self.bottle = bottle
        self.raw_repos = dict(repos)
        self.kinds = tuple(kinds)
        self.chain_forges = dict(chain_forges or {})
        self.extends = extends
        self.forge_bottles = dict(forge_bottles or {})
        chain = {host: replace(homes[0], bottle="")
                 for host, homes in forges_by_host([("", {"git-gate": {
                     "forges": self.chain_forges}})]).items()}
        self.plan = plan(bottle, old_repos(repos, kinds), extends, chain, self.forge_bottles)
        super().__init__(explain(self.plan, extends, repos))

    def explained(self, *, extends: object = None,
                  forge_bottles: Mapping[str, Sequence[ForgeHome]] | None = None,
                  ) -> "OldGitGateFormatError":
        """This error, knowing the bottle's `extends` and the forge bottles
        this host has."""
        return OldGitGateFormatError(
            self.bottle, self.raw_repos, self.kinds, chain_forges=self.chain_forges,
            extends=extends if extends is not None else self.extends,
            forge_bottles=forge_bottles if forge_bottles is not None else self.forge_bottles)


__all__ = ["DOCS", "ForgeHome", "OldGitGateFormatError", "OldRepo", "Plan", "explain",
           "extends_list", "flow_list", "forges_by_host", "hand_edit", "list_form", "new_extends",
           "old_repos", "pick_forge", "plan", "scan_bottles"]
